"""Code Reviewer Agent -- graph node `code_reviewer_agent`.

Spec: `docs/agents/04-code-reviewer-agent.md`.

Independent quality gate between the Developer and QA. Judges the PR against the
Architect's technical design plus objective SonarQube results -- never against
business/acceptance correctness (that is QA's job).

`review_status` is PASS only if the manual review has no BLOCKER/MAJOR findings
AND the SonarQube quality gate passes. A PASS is the only event that merges a PR.

Reads:  pr, architecture_doc_ref, dev_plan_ref, review_iterations (context only)
Writes: review_status, review_findings, sonarqube_report_ref, pr.status,
        stories[current].status
Tools:  GitHub + SonarQube. No Jira -- bug filing belongs to QA.

Reports status honestly every round; it never tracks or enforces
MAX_REVIEW_ITERATIONS itself -- LangGraph owns that.
"""

from __future__ import annotations

import logging
from typing import Any

from agents import (
    AgentOutputError,
    as_int,
    find_story,
    invoke_agent_json,
    replace_story,
)
from config.settings import REVIEW_DIFF_LIMIT
from core.prompts import CODE_REVIEWER_SYSTEM_PROMPT
from core.state import BLOCKING_SEVERITIES, Finding, WorkflowState
from tools import get_github, get_sonarqube

logger = logging.getLogger(__name__)

AGENT_NAME = "code_reviewer_agent"

_VALID_SEVERITIES = {"BLOCKER", "MAJOR", "MINOR", "INFO"}

#: Values a model echoes back out of the schema example instead of writing a
#: real finding. Anything here is noise, not a defect.
_PLACEHOLDER_TEXT = {"string", "message", "finding", "n/a", "none", "todo",
                     "src/api/user.py line 42", "example"}


def _normalize_findings(raw_findings: Any) -> list[Finding]:
    """Coerce the model's findings into the schema, dropping unusable entries.

    Two rules matter beyond shape:

    * Every finding from here is tagged `CODE_REVIEWER`, whatever the model
      said. SONARQUBE findings come from the scanner and are appended by the
      caller; letting the model claim that source would let it fabricate a
      static-analysis result that then gates a merge. Observed live: a 3B model
      emitted `{"source": "SONARQUBE", ...}` copied straight out of the schema
      example while the scanner had returned nothing at all.
    * A finding whose message is a schema placeholder is dropped. It is not
      actionable, and it would otherwise block the story forever with no way for
      the Developer to address it.
    """
    if not isinstance(raw_findings, list):
        return []

    findings: list[Finding] = []
    for raw in raw_findings:
        if not isinstance(raw, dict):
            continue
        message = str(raw.get("message", "")).strip()
        file = str(raw.get("file", "")).strip()
        if not message or not file:
            logger.warning("[%s] dropped finding without file/message: %r", AGENT_NAME, raw)
            continue
        if message.lower() in _PLACEHOLDER_TEXT or file.lower() in _PLACEHOLDER_TEXT:
            logger.warning(
                "[%s] dropped placeholder finding copied from the schema: %r",
                AGENT_NAME,
                raw,
            )
            continue

        claimed_source = str(raw.get("source", "")).strip().upper()
        if claimed_source == "SONARQUBE":
            logger.warning(
                "[%s] re-tagged a model finding that claimed SONARQUBE as its source; "
                "scanner findings come only from the scanner",
                AGENT_NAME,
            )
        severity = str(raw.get("severity", "")).strip().upper()

        line_raw = raw.get("line")
        line = None if line_raw in (None, "", "null") else as_int(line_raw, 0) or None

        findings.append(
            Finding(
                source="CODE_REVIEWER",
                severity=severity if severity in _VALID_SEVERITIES else "MAJOR",
                file=file,
                line=line,
                message=message,
            )
        )
    return findings


def code_reviewer_node(state: WorkflowState) -> dict:
    """Review the open PR and return a partial state update.

    Runs identically regardless of `pr.kind` -- a QA-defect FIX PR gets the same
    full review as a first-pass FEATURE PR.
    """
    pr = state["pr"]
    if pr is None:
        raise AgentOutputError("code_reviewer_agent invoked with no PR to review")

    story_id = state["current_story_id"]
    story = find_story(list(state["stories"]), story_id)
    if story is None:
        raise AgentOutputError(f"code_reviewer_agent has no current story ({story_id!r})")

    github = get_github()
    sonarqube = get_sonarqube()
    branch = state["branch_name"]

    diff = github.get_pull_request_diff(pr["id"])
    truncated = len(diff) > REVIEW_DIFF_LIMIT
    if truncated:
        logger.warning(
            "[%s] diff is %d chars; truncating to %d for review",
            AGENT_NAME,
            len(diff),
            REVIEW_DIFF_LIMIT,
        )

    # SonarQube runs before the model so its objective findings are available as
    # context for the manual review, not just merged into the verdict after.
    scan_id = sonarqube.trigger_scan(branch)
    gate_passed, sonar_findings = sonarqube.get_quality_gate_result(scan_id)

    context = {
        "pr": pr,
        "branch_name": branch,
        "pull_request_diff": diff[:REVIEW_DIFF_LIMIT],
        "diff_truncated": truncated,
        "architecture_doc_ref": state["architecture_doc_ref"],
        "dev_plan_ref": state["dev_plan_ref"],
        "story_under_review": {
            "id": story["id"],
            "title": story["title"],
            "requires_ui": story["requires_ui"],
            "requires_api": story["requires_api"],
        },
        "sonarqube": {
            "quality_gate_passed": gate_passed,
            "findings": sonar_findings,
            "simulated": sonarqube.dry_run,
        },
        # Informational only -- the limit itself is LangGraph's to enforce.
        "review_iterations": state["review_iterations"],
    }

    result = invoke_agent_json(AGENT_NAME, CODE_REVIEWER_SYSTEM_PROMPT, context)

    findings = _normalize_findings(result.get("review_findings"))
    findings.extend(sonar_findings)
    blocking = [f for f in findings if f["severity"] in BLOCKING_SEVERITIES]

    # The model states a verdict, but the spec's rule is objective, so it is
    # recomputed here: PASS requires no BLOCKER/MAJOR finding AND a passing gate.
    # MINOR/INFO findings are reported without failing the gate.
    reported = str(result.get("review_status", "")).strip().upper()
    review_status = "PASS" if not blocking and gate_passed else "FAIL"
    if reported and reported != review_status:
        logger.warning(
            "[%s] model reported %s; recomputed %s from %d blocking finding(s), sonar_gate=%s",
            AGENT_NAME,
            reported,
            review_status,
            len(blocking),
            gate_passed,
        )

    if review_status == "FAIL":
        logger.info(
            "[%s] FAIL on %s: %d finding(s), %d blocking",
            AGENT_NAME,
            pr["id"],
            len(findings),
            len(blocking),
        )
        github.post_review_comment(pr["id"], findings)
        return {
            "review_status": "FAIL",
            "review_findings": findings,
            "sonarqube_report_ref": scan_id,
            "pr": dict(pr, status="OPEN"),
            "stories": replace_story(list(state["stories"]), dict(story, status="IN_REVIEW")),
        }

    # PASS is the only event that merges, and this agent is the only actor
    # permitted to perform it (Merge Control, spec section 10).
    logger.info("[%s] PASS on %s -- merging", AGENT_NAME, pr["id"])
    merged = github.merge_pull_request(pr["id"])
    return {
        "review_status": "PASS",
        "review_findings": [],
        "sonarqube_report_ref": scan_id,
        "pr": dict(pr, status=merged["status"]),
        "stories": replace_story(list(state["stories"]), dict(story, status="IN_QA")),
    }


__all__ = ["AGENT_NAME", "code_reviewer_node"]
