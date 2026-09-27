"""QA Agent -- graph node `qa_agent`.

Spec: `docs/agents/05-qa-agent.md`.

Final validation gate. Tests the *merged* implementation against the ORIGINAL
Jira acceptance criteria written by the PM Agent -- re-read from Jira, never
re-derived from the code or the PR description.

Adaptive scope, driven strictly by the Architect's flags:
  requires_api -> API tests against the merged endpoints
  requires_ui  -> additionally Playwright UI tests; when false, Playwright is not
                  constructed or invoked at all
  always       -> relevant regression tests

Any single unmet acceptance criterion is a FAIL -- QA never partially passes a
story, and every FAIL must file a linked Jira Bug for traceability.

Reads:  current_story_id (+ that story's acceptance criteria from Jira),
        requires_ui, requires_api, pr, repo
Writes: qa_status, qa_test_type, qa_results_ref, jira_bug_id,
        stories[current].status
Tools:  Playwright (conditional), API/regression test runner, Jira.

Evidence policy: executed test results are authoritative. The model is used to
map failures onto the criteria they violate, never to overturn a red test. When
a story has no executable spec at all, `QA_FAIL_WITHOUT_SPECS` decides whether
that is a failure (default) or falls back to the model's judgment.
"""

from __future__ import annotations

import json
import logging

from agents import (
    AgentOutputError,
    as_str_list,
    find_story,
    invoke_agent_json,
    replace_story,
)
from config.settings import QA_FAIL_WITHOUT_SPECS
from core.prompts import QA_SYSTEM_PROMPT
from core.state import QATestType, WorkflowState
from tools import get_api_runner, get_jira, get_playwright

logger = logging.getLogger(__name__)

AGENT_NAME = "qa_agent"


def _test_type(requires_ui: bool, requires_api: bool) -> QATestType:
    """Adaptive test-type selection, straight from the Architect's flags.

    `qa_test_type` is a single value in the schema, so it records the broadest
    suite exercised. Regression always runs alongside whichever is reported; UI
    outranks API because a UI story exercises its API through the interface.
    """
    if requires_ui:
        return "UI"
    if requires_api:
        return "API"
    return "REGRESSION"


def qa_node(state: WorkflowState) -> dict:
    """Test the merged story and return a partial state update.

    Reports PASS/FAIL honestly; MAX_QA_ITERATIONS and the "remaining READY
    stories?" branch are both LangGraph's to decide.
    """
    story_id = state["current_story_id"]
    story = find_story(list(state["stories"]), story_id)
    if story is None:
        raise AgentOutputError(f"qa_agent has no current story ({story_id!r})")

    requires_ui = story["requires_ui"]
    requires_api = story["requires_api"]
    qa_test_type = _test_type(requires_ui, requires_api)

    jira = get_jira()
    # The PM's originals, read from the tool of record -- never the developer's
    # interpretation and never a copy carried through state.
    acceptance_criteria = jira.read_story_acceptance_criteria(story_id)
    if not acceptance_criteria:
        logger.warning(
            "[%s] %s has no acceptance criteria in Jira -- nothing to test against",
            AGENT_NAME,
            story_id,
        )

    api_runner = get_api_runner()
    suites: dict[str, dict] = {}

    if requires_api:
        suites["api"] = api_runner.run(story_id)
    else:
        logger.info("[%s] %s does not require an API; API tests skipped", AGENT_NAME, story_id)

    if requires_ui:
        # Playwright is constructed only on this branch -- a backend-only story
        # must not reach it at all (adaptive scope).
        logger.info("[%s] %s requires UI testing (Playwright)", AGENT_NAME, story_id)
        suites["ui"] = get_playwright().run_ui_tests(story_id)
    else:
        logger.info("[%s] %s is backend-only; Playwright bypassed", AGENT_NAME, story_id)

    done_story_ids = [s["id"] for s in state["stories"] if s["status"] == "DONE"]
    suites["regression"] = api_runner.run_regression(done_story_ids)

    executed = [name for name, outcome in suites.items() if not outcome.get("skipped")]
    failures = [
        {**failure, "suite": name}
        for name, outcome in suites.items()
        for failure in outcome.get("failures", [])
    ]

    result = invoke_agent_json(
        AGENT_NAME,
        QA_SYSTEM_PROMPT,
        {
            "current_story_id": story_id,
            "story": {
                "id": story["id"],
                "title": story["title"],
                "requires_ui": requires_ui,
                "requires_api": requires_api,
            },
            "acceptance_criteria": acceptance_criteria,
            "qa_test_type": qa_test_type,
            "suites_executed": executed,
            "suites_skipped": [n for n, o in suites.items() if o.get("skipped")],
            "test_failures": failures,
            "pr": state["pr"],
            "repo": state["repo"],
            "previously_done_stories_for_regression": suites["regression"].get("covered", []),
            # Informational only -- the limit itself is LangGraph's to enforce.
            "qa_iterations": state["qa_iterations"],
        },
    )

    model_failed_criteria = as_str_list(result.get("failed_criteria"))
    reported = str(result.get("qa_status", "")).strip().upper()

    # Executed results are authoritative. The model contributes the mapping from
    # a red test to the criterion it violates, and its own judgment only decides
    # cases no suite could settle.
    if failures:
        qa_status = "FAIL"
        failed_criteria = model_failed_criteria or [
            f"{failure.get('criterion', failure.get('name', 'unknown'))}"
            for failure in failures
        ]
    elif not executed:
        if QA_FAIL_WITHOUT_SPECS:
            qa_status = "FAIL"
            failed_criteria = [
                "No executable acceptance test was found for this story, so none of "
                "its acceptance criteria could be verified."
            ]
            logger.warning(
                "[%s] %s has no executable spec; failing rather than passing unverified "
                "(set QA_FAIL_WITHOUT_SPECS=false to allow the model's judgment to stand)",
                AGENT_NAME,
                story_id,
            )
        else:
            qa_status = "FAIL" if (reported == "FAIL" or model_failed_criteria) else "PASS"
            failed_criteria = model_failed_criteria
            logger.warning(
                "[%s] %s has no executable spec; falling back to the model's judgment (%s)",
                AGENT_NAME,
                story_id,
                qa_status,
            )
    else:
        # Every executed suite is green. A model claiming failure anyway still
        # counts -- any single unmet criterion is a FAIL, and it may have spotted
        # a criterion the specs do not cover.
        qa_status = "FAIL" if (reported == "FAIL" and model_failed_criteria) else "PASS"
        failed_criteria = model_failed_criteria if qa_status == "FAIL" else []

    evidence = json.dumps(suites, indent=2, default=str)
    qa_results_ref = f"qa-run/{story_id}/{state['qa_iterations'] + 1}"

    if qa_status == "FAIL":
        # Every FAIL must leave a linked defect behind for traceability, even
        # when the workflow is about to halt on MAX_QA_ITERATIONS.
        description = (
            f"QA failed for {story_id} ({qa_test_type}).\n\n"
            "Unmet acceptance criteria:\n"
            + "\n".join(f"- {item}" for item in failed_criteria)
        )
        bug_id = jira.create_bug(story_id, description, evidence[:30000])
        logger.info(
            "[%s] FAIL on %s (%s): %d unmet criterion/criteria -> bug %s",
            AGENT_NAME,
            story_id,
            qa_test_type,
            len(failed_criteria),
            bug_id,
        )
        jira.transition_story_status(story_id, "BLOCKED")
        return {
            "qa_status": "FAIL",
            "qa_test_type": qa_test_type,
            "qa_results_ref": qa_results_ref,
            "jira_bug_id": bug_id,
            "stories": replace_story(list(state["stories"]), dict(story, status="BLOCKED")),
        }

    logger.info(
        "[%s] PASS on %s (%s) -- suites executed: %s",
        AGENT_NAME,
        story_id,
        qa_test_type,
        executed,
    )
    jira.transition_story_status(story_id, "DONE")
    return {
        "qa_status": "PASS",
        "qa_test_type": qa_test_type,
        "qa_results_ref": qa_results_ref,
        "jira_bug_id": None,
        "stories": replace_story(list(state["stories"]), dict(story, status="DONE")),
    }


__all__ = ["AGENT_NAME", "qa_node"]
