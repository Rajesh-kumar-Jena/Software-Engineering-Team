"""Developer Agent -- graph nodes `select_ready_story` and `developer_agent`.

Spec: `docs/agents/03-developer-agent.md`.

The only agent permitted to write to the repository. Implements exactly one Jira
story at a time and has three structurally distinct entry points:

  2a. Fresh story  -> new feature branch, implement, self-validate, open a PR.
  2b. Review FAIL  -> fix on the SAME branch, push to the SAME PR (never a new one).
  2c. QA FAIL      -> NEW fix branch, implement fix, NEW PR (kind="FIX"), which
                      re-enters code review from the top.

Reads:  stories, architecture_doc_ref, dev_plan_ref, human_approval.status, repo,
        pr + review_findings (2b), jira_bug_id + qa_results_ref (2c)
Writes: current_story_id, stories[current].status, branch_name, pr
Tools:  GitHub + Jira. No SonarQube, no Playwright.

Hard constraints: never act while `human_approval.status != "APPROVED"`; never
merge its own PR; never build UI when `requires_ui` is false.

Self-validation outcome is reported through the story's status -- `IN_REVIEW`
means a PR is up for review, `IN_PROGRESS` means validation failed. `core/graph.py`
reads that and owns the `MAX_DEV_ATTEMPTS` decision; this agent never counts.

On executing generated code: the spec's step is "run the application and unit
tests". Doing that runs model-authored code on this machine, so it is gated
behind `DEV_RUN_TESTS` (off by default). With it off, self-validation is a parse
check over every generated file plus the model's own reported judgment -- weaker
than the spec, and `self_validation.method` in the logs says which ran.
"""

from __future__ import annotations

import ast
import logging
import shlex
import subprocess
from pathlib import Path

from agents import (
    AgentOutputError,
    find_story,
    invoke_agent_json,
    replace_story,
)
from config.settings import (
    DEV_CONTEXT_MAX_CHARS,
    DEV_CONTEXT_MAX_FILES,
    DEV_RUN_TESTS,
    DEV_TEST_COMMAND,
    DEV_WORKSPACE,
    GITHUB_DEFAULT_BRANCH,
)
from core.prompts import DEVELOPER_SYSTEM_PROMPT
from core.state import PullRequestRef, WorkflowState
from tools import get_github, get_jira

logger = logging.getLogger(__name__)

AGENT_NAME = "developer_agent"

#: Statuses that mean a story is already claimed by an in-flight cycle, so the
#: READY scan must not pick it up again.
_CLAIMED = frozenset({"IN_PROGRESS", "IN_REVIEW", "IN_QA", "DONE"})


def _entry_mode(state: WorkflowState) -> str:
    """Which of the spec's three entry points this invocation is.

    QA is checked first: a QA failure supersedes any stale review status left
    over from the review round that merged the defective code.
    """
    if state["qa_status"] == "FAIL" and state["jira_bug_id"]:
        return "qa_fix"
    if state["review_status"] == "FAIL" and state["pr"] is not None:
        return "review_fix"
    return "feature"


def select_ready_story_node(state: WorkflowState) -> dict:
    """Deterministic backlog scan -- graph node `select_ready_story`.

    Per the spec's section 5: candidates are stories not yet DONE whose
    `business_dependencies` *and* `technical_dependencies` are all DONE; the
    single highest-priority (lowest `priority`) candidate becomes
    `current_story_id`. If none qualify, `current_story_id` is left None and
    LangGraph routes to BACKLOG_COMPLETE.

    This is deliberately non-LLM: selection is a deterministic local decision.
    """
    if state["human_approval"]["status"] != "APPROVED":
        # Defence in depth -- the graph cannot reach this node unapproved, but
        # the approval gate is a hard system boundary, so it is asserted here too.
        raise PermissionError(
            "select_ready_story reached without human approval "
            f"(status={state['human_approval']['status']})"
        )

    stories = [dict(story) for story in state["stories"]]
    done_ids = {story["id"] for story in stories if story["status"] == "DONE"}

    ready: list[dict] = []
    for story in stories:
        if story["status"] in _CLAIMED:
            continue
        dependencies = set(story["business_dependencies"]) | set(
            story["technical_dependencies"]
        )
        unmet = dependencies - done_ids
        if unmet:
            logger.debug(
                "[select_ready_story] %s not ready; unmet dependencies %s",
                story["id"],
                sorted(unmet),
            )
            continue
        story["status"] = "READY"
        ready.append(story)

    if not ready:
        logger.info("[select_ready_story] no READY story remains")
        return {"stories": stories, "current_story_id": None}

    # Lower priority number = higher priority; story id breaks ties so selection
    # is reproducible across runs.
    selected = min(ready, key=lambda story: (story["priority"], story["id"]))
    selected["status"] = "IN_PROGRESS"
    logger.info(
        "[select_ready_story] selected %s (P%s) %s",
        selected["id"],
        selected["priority"],
        selected["title"],
    )
    get_jira().transition_story_status(selected["id"], "IN_PROGRESS")

    # A new story starts a clean cycle: per-story pointers and results from the
    # previous story must not leak into it. The counters those pointers gate
    # (`dev_attempts`, `qa_iterations`) are reset by `core/graph.py`, which owns them.
    return {
        "stories": replace_story(stories, selected),
        "current_story_id": selected["id"],
        "branch_name": None,
        "pr": None,
        "review_status": None,
        "review_findings": [],
        "sonarqube_report_ref": None,
        "qa_status": None,
        "qa_test_type": None,
        "qa_results_ref": None,
        "jira_bug_id": None,
    }


def _branch_name(mode: str, story_id: str, state: WorkflowState) -> str:
    """Deterministic branch name for the current entry mode."""
    if mode == "review_fix":
        existing = state["branch_name"]
        if not existing:
            raise AgentOutputError(
                "review-fix re-entry has no branch_name to update -- refusing to "
                "open a second branch for a code-review finding"
            )
        return existing
    if mode == "qa_fix":
        # A distinct branch per QA round, so successive fixes never collide.
        return f"fix/{story_id}-{state['qa_iterations'] + 1}"
    return f"feature/{story_id}"


def _repository_context(github, branch: str, state: WorkflowState) -> tuple[list[str], dict[str, str]]:
    """What already exists in the repository the Developer is about to edit.

    Returns `(all tracked paths, contents of the most relevant ones)`. The tree
    is cheap and complete; contents are bounded by `DEV_CONTEXT_MAX_FILES` and
    `DEV_CONTEXT_MAX_CHARS` because they share the prompt with the design
    document and the acceptance criteria.

    Source files are preferred over everything else, since those carry the
    imports and contracts a new file has to honour.
    """
    try:
        paths = github.list_repository_files(branch)
    except Exception as exc:  # noqa: BLE001 -- degrade to blind authoring, do not halt
        logger.warning("[%s] could not list the repository: %s", AGENT_NAME, exc)
        return [], {}

    if not paths:
        return [], {}

    def rank(path: str) -> tuple[int, int]:
        # Package markers and modules first -- they define what imports resolve.
        name = path.rsplit("/", 1)[-1]
        return (
            0 if name == "__init__.py" else 1 if path.endswith(".py") else 2,
            len(path),
        )

    contents: dict[str, str] = {}
    budget = DEV_CONTEXT_MAX_CHARS
    for path in sorted(paths, key=rank)[:DEV_CONTEXT_MAX_FILES]:
        if budget <= 0:
            break
        try:
            body = github.read_repository_file(path, branch)
        except Exception as exc:  # noqa: BLE001 -- one unreadable file is not fatal
            logger.warning("[%s] could not read %s: %s", AGENT_NAME, path, exc)
            continue
        if not body:
            continue
        contents[path] = body[:budget]
        budget -= len(contents[path])

    logger.info(
        "[%s] repository context: %d file(s) tracked, %d shown",
        AGENT_NAME,
        len(paths),
        len(contents),
    )
    return paths, contents


def _safe_files(raw: object) -> dict[str, str]:
    """Validate the model's `files` object into a path -> content mapping.

    Paths are constrained to the repository: absolute paths and any `..`
    traversal are refused rather than silently rewritten, because a path is the
    one part of this payload that decides *where* a write lands.
    """
    if not isinstance(raw, dict) or not raw:
        raise AgentOutputError(
            "developer_agent must return a non-empty 'files' object mapping paths to content"
        )

    files: dict[str, str] = {}
    for path, content in raw.items():
        candidate = str(path).strip().replace("\\", "/")
        if not candidate:
            continue
        pure = Path(candidate)
        if pure.is_absolute() or ".." in pure.parts or candidate.startswith("/"):
            raise AgentOutputError(f"developer_agent proposed an unsafe path: {path!r}")
        files[candidate] = content if isinstance(content, str) else str(content)

    if not files:
        raise AgentOutputError("developer_agent returned no usable files")
    return files


def _self_validate(files: dict[str, str], reported_pass: bool) -> tuple[bool, str, str]:
    """Run the Developer's own validation over the generated files.

    Returns `(passed, method, detail)`. A parse error is an objective failure and
    overrides a model that claims PASS; the model's judgment only decides cases
    the checks cannot.
    """
    for path, content in files.items():
        if not path.endswith(".py"):
            continue
        try:
            ast.parse(content, filename=path)
        except SyntaxError as exc:
            return False, "syntax", f"{path}: {exc}"

    if not DEV_RUN_TESTS:
        return (
            reported_pass,
            "syntax+model-judgment",
            "parse check passed; DEV_RUN_TESTS is off so the app and unit tests were not run",
        )

    workspace = Path(DEV_WORKSPACE)
    for path, content in files.items():
        target = workspace / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    try:
        completed = subprocess.run(
            shlex.split(DEV_TEST_COMMAND),
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, "tests", f"could not run {DEV_TEST_COMMAND!r}: {exc}"

    if completed.returncode != 0:
        return False, "tests", completed.stdout[-2000:] + completed.stderr[-2000:]
    return True, "tests", f"{DEV_TEST_COMMAND} passed"


def developer_node(state: WorkflowState) -> dict:
    """Implement the current story and return a partial state update.

    Dispatches on entry point: fresh (2a), review-fix (2b), or QA-fix (2c).
    """
    approval = state["human_approval"]
    if approval["status"] != "APPROVED":
        # The approval gate is absolute: no repository-mutating work may be
        # reachable before a human approves the technical design.
        raise PermissionError(
            f"developer_agent blocked: human_approval.status={approval['status']!r}"
        )

    story_id = state["current_story_id"]
    story = find_story(list(state["stories"]), story_id)
    if story is None:
        raise AgentOutputError(f"developer_agent has no current story ({story_id!r})")

    mode = _entry_mode(state)
    github = get_github()
    jira = get_jira()
    logger.info("[%s] entry mode %s for %s", AGENT_NAME, mode, story_id)

    # The architecture doc is the authoritative spec for HOW to implement, so it
    # is read from Jira rather than reconstructed from the ref alone.
    architecture = ""
    if state["architecture_doc_ref"]:
        try:
            architecture = jira.read_architecture_doc(state["architecture_doc_ref"])
        except Exception as exc:  # noqa: BLE001 -- degrade, do not halt the story
            logger.warning("[%s] could not read the architecture doc: %s", AGENT_NAME, exc)

    # Branch before code, exactly as the diagram orders it. Re-entry on the same
    # feature branch is a reuse, not an error.
    branch = _branch_name(mode, story_id, state)
    github.create_branch(branch, from_ref=state["repo"]["default_branch"] or GITHUB_DEFAULT_BRANCH)

    # The Developer authors whole files, so it has to see what it must stay
    # compatible with. Without this it writes blind: rewriting a module another
    # file imports from, or using a framework the repository does not use. Each
    # fix then breaks a different cross-file contract, the reviewer correctly
    # reports the new breakage, and the review loop diverges instead of
    # converging. Read from the branch so a review fix sees its own last commit.
    repository_files, existing_files = _repository_context(github, branch, state)

    context: dict = {
        "entry_mode": {
            "feature": "fresh story (2a)",
            "review_fix": "code review FAIL -- update the SAME PR (2b)",
            "qa_fix": "QA FAIL -- NEW fix branch and NEW PR (2c)",
        }[mode],
        "current_story": story,
        "acceptance_criteria": jira.read_story_acceptance_criteria(story_id),
        "architecture_document": architecture,
        "dev_plan_ref": state["dev_plan_ref"],
        "human_approval.status": approval["status"],
        "repo": state["repo"],
        "branch_name": branch,
        # Informational: the agent reports its own attempt honestly, it does not
        # decide whether another one is allowed.
        "dev_attempts_so_far": state["dev_attempts"],
        "scope": {
            "build_backend": story["requires_api"],
            "build_ui": story["requires_ui"],
        },
        # Stay compatible with what is already here: reuse these modules and
        # their conventions rather than re-creating or contradicting them.
        "repository_files": repository_files,
        "existing_file_contents": existing_files,
    }
    if mode == "review_fix":
        context["pr"] = state["pr"]
        context["review_findings"] = state["review_findings"]
    elif mode == "qa_fix":
        context["jira_bug_id"] = state["jira_bug_id"]
        context["qa_results_ref"] = state["qa_results_ref"]
        context["pr_that_was_merged"] = state["pr"]

    def _validate(candidate: dict) -> None:
        """Structural checks, run INSIDE the invoke loop so they can be retried.

        These are faults a model corrects when told about them -- an empty
        'files' object, a missing 'self_validation'. Raised after the loop they
        would halt the whole run on a single bad reply, with two unused attempts
        still on the table.
        """
        if not isinstance(candidate.get("self_validation"), dict):
            raise AgentOutputError(
                "output must contain a 'self_validation' object with a 'status' of PASS or FAIL"
            )
        _safe_files(candidate.get("files"))

    result = invoke_agent_json(
        AGENT_NAME, DEVELOPER_SYSTEM_PROMPT, context, validate=_validate
    )

    validation = result["self_validation"]
    reported_pass = str(validation.get("status", "")).strip().upper() == "PASS"

    files = _safe_files(result.get("files"))
    passed, method, detail = _self_validate(files, reported_pass)
    logger.info(
        "[%s] self-validation %s (%s) for %s: %s",
        AGENT_NAME,
        "PASS" if passed else "FAIL",
        method,
        story_id,
        detail[:300],
    )

    if not passed:
        # Nothing is committed on a failed attempt. The story stays IN_PROGRESS,
        # which is how `core/graph.py` detects it and decides whether
        # MAX_DEV_ATTEMPTS allows another one.
        updated = dict(story, status="IN_PROGRESS")
        return {
            "stories": replace_story(list(state["stories"]), updated),
            "current_story_id": story_id,
            "branch_name": branch,
        }

    commit_message = {
        "feature": f"{story_id}: {story['title']}",
        "review_fix": f"{story_id}: address code review findings",
        "qa_fix": f"{story_id}: fix {state['jira_bug_id']}",
    }[mode]
    github.commit(branch, commit_message, files)
    github.push(branch)

    if mode == "review_fix":
        existing = state["pr"]
        assert existing is not None  # guaranteed by _entry_mode
        refreshed = github.update_pull_request(existing["id"], branch, commit_message)
        # Identity is preserved deliberately: a review fix must land on the same
        # PR, so only its status is taken from the refreshed copy.
        pr = PullRequestRef(
            id=existing["id"],
            url=existing["url"],
            story_id=existing["story_id"],
            kind=existing["kind"],
            status=refreshed["status"],
        )
    else:
        opened = github.open_pull_request(
            branch=branch,
            title=commit_message,
            body=(
                f"Implements {story_id}: {story['title']}"
                if mode == "feature"
                else f"Fixes {state['jira_bug_id']} on {story_id}"
            ),
            story_id=story_id,
        )
        pr = PullRequestRef(
            id=opened["id"],
            url=opened["url"],
            story_id=story_id,
            kind="FIX" if mode == "qa_fix" else "FEATURE",
            status=opened["status"],
        )

    jira.transition_story_status(story_id, "IN_REVIEW")
    updated = dict(story, status="IN_REVIEW")
    logger.info(
        "[%s] %s ready for review on %s (pr=%s, kind=%s)",
        AGENT_NAME,
        story_id,
        branch,
        pr["id"],
        pr["kind"],
    )

    return {
        "stories": replace_story(list(state["stories"]), updated),
        "current_story_id": story_id,
        "branch_name": branch,
        "pr": pr,
        # Awaiting a fresh verdict; last round's findings no longer apply.
        "review_status": "PENDING",
        "review_findings": [],
    }


__all__ = ["AGENT_NAME", "select_ready_story_node", "developer_node"]
