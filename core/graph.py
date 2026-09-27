"""LangGraph nodes and conditional routing edges.

Spec: `docs/agents/00-orchestration-and-routing.md`.

This module owns every workflow-level decision. Per the Decision Principle:
agents make local decisions ("is this code acceptable?"); LangGraph makes routing
decisions ("where does execution go next?"). No agent module re-implements
anything here -- in particular, the retry counters are compared *here*, never by
the agent whose loop they gate.

Nodes (section 1):
    pm_agent, solution_architect_agent, human_approval_gate, select_ready_story,
    developer_agent, code_reviewer_agent, qa_agent

Edges (section 2), abbreviated:
    START -> pm_agent -> solution_architect_agent -> human_approval_gate
    human_approval_gate:  APPROVED -> select_ready_story
                          REJECTED -> solution_architect_agent
                          PENDING  -> interrupt, wait for an external signal
    select_ready_story:   story found -> developer_agent; none -> BACKLOG_COMPLETE -> END
    developer_agent:      self-validation FAIL & under MAX_DEV_ATTEMPTS -> developer_agent
                          PR opened -> code_reviewer_agent
    code_reviewer_agent:  FAIL & under MAX_REVIEW_ITERATIONS -> developer_agent (SAME PR)
                          PASS -> merge -> qa_agent
    qa_agent:             FAIL & under MAX_QA_ITERATIONS -> Jira Bug -> developer_agent
                                                            (NEW fix branch + PR)
                          PASS -> select_ready_story
    Any hard-limit breach -> workflow_status = HALTED -> END (state preserved,
    nothing rolled back, human resumes manually).

Structure of this module
------------------------
Each of the seven nodes is a thin wrapper around the agent function that does the
work. The wrapper is where orchestration-owned bookkeeping lives -- counter
increments and resets, `workflow_status`, and halt entries -- so the agents stay
free of routing concerns and of the counters that gate them.

How a node's outcome reaches its router: routers cannot write state, so a
wrapper records the outcome in the schema fields that already carry it
(`workflow_status`, `review_status`, `qa_status`, `current_story_id`, and the
current story's own status) and the router reads those back after the merge.
"""

from __future__ import annotations

import logging
from typing import Callable

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.types import Checkpointer, interrupt

from agents.code_reviewer import code_reviewer_node
from agents.developer import developer_node, select_ready_story_node
from agents.product_manager import pm_agent_node
from agents.qa_agent import qa_node
from agents.solution_architect import solution_architect_node
from core.state import (
    ApprovalState,
    ErrorEntry,
    ErrorKind,
    WorkflowState,
    WorkflowStatus,
    new_error_entry,
)
from agents import AgentOutputError
from tools import ToolFailure

logger = logging.getLogger(__name__)

__all__ = [
    "build_graph",
    "DEFAULT_RECURSION_LIMIT",
    "WorkflowState",
]

#: The backlog loop re-enters `select_ready_story` once per story, and each story
#: can burn several developer/review/QA rounds, so LangGraph's default of 25
#: super-steps is far too low for a real backlog.
DEFAULT_RECURSION_LIMIT = 250


# ---------------------------------------------------------------------------
# Halt bookkeeping (section 3)
# ---------------------------------------------------------------------------


def _halt(
    state: WorkflowState,
    agent: str,
    reason: str,
    kind: ErrorKind = "HARD_LIMIT",
) -> dict:
    """Terminate safely: record why, preserve everything, roll nothing back.

    The full state -- Jira/GitHub pointers, findings, results -- survives so a
    human can pick the run up manually.
    """
    logger.error("[graph] HALTED (%s): %s", kind, reason)
    error_log: list[ErrorEntry] = list(state["error_log"])
    error_log.append(new_error_entry(agent=agent, kind=kind, detail=reason))
    return {
        "workflow_status": WorkflowStatus.HALTED.value,
        "halt_reason": reason,
        "error_log": error_log,
    }


def _guard(state: WorkflowState, agent: str, run: Callable[[], dict]) -> dict:
    """Run an agent node, converting any unrecoverable failure into a safe halt.

    This is the workflow's safe-termination boundary (section 3): a run ends by
    recording why and preserving state, never by raising through LangGraph. The
    three classes map onto the schema's `ErrorKind` values:

    * `ToolFailure` -> TOOL_FAILURE. `MAX_TOOL_RETRIES` is spent inside the tool
      layer (`tools.with_retries`); what reaches here is the exhausted-or-
      non-transient outcome. Kept distinct so an outage is never recorded as a
      genuine review or QA verdict.
    * `AgentOutputError` -> VALIDATION_FAILURE. The agent could not get valid
      structured output from the model even after its own reprompts. Halting is
      right: LangGraph routes on those fields, so guessing a default would send
      the workflow down a path nothing actually decided.
    * `PermissionError` -> HARD_LIMIT. A system boundary was breached (acting
      before human approval). Nothing about that is retryable.
    """
    try:
        return run()
    except ToolFailure as exc:
        return _halt(
            state,
            agent,
            f"{exc.tool} exhausted its retry budget after {exc.attempts} attempt(s) "
            f"on story {state['current_story_id'] or 'n/a'}: {exc.detail}",
            kind="TOOL_FAILURE",
        )
    except AgentOutputError as exc:
        return _halt(
            state,
            agent,
            f"{agent} could not produce valid structured output on story "
            f"{state['current_story_id'] or 'n/a'}: {exc}",
            kind="VALIDATION_FAILURE",
        )
    except PermissionError as exc:
        return _halt(state, agent, f"{agent} breached a system boundary: {exc}")
    except GraphBubbleUp:
        # LangGraph's own control flow (interrupts, parent commands) travels by
        # exception. It is not a failure and must reach the runtime untouched.
        raise
    except Exception as exc:  # noqa: BLE001 -- deliberate last line of defence
        # Anything unanticipated still ends the run the same way: recorded and
        # preserved, never a traceback out of a node. A workflow that reaches
        # here has a bug, so the traceback is logged in full -- but the run is
        # halted safely rather than aborted, which is the whole point of
        # section 3's safe-termination rule.
        logger.exception("[graph] unhandled error in %s", agent)
        return _halt(
            state,
            agent,
            f"{agent} raised an unhandled {type(exc).__name__} on story "
            f"{state['current_story_id'] or 'n/a'}: {exc}",
            kind="VALIDATION_FAILURE",
        )


def _is_halt(update: dict) -> bool:
    """Whether `_guard` turned this update into a halt."""
    return update.get("workflow_status") == WorkflowStatus.HALTED.value


def _limit_breach_reason(counter: str, value: int, limit: int, story_id: str | None) -> str:
    return (
        f"{counter} reached its hard limit ({value}/{limit}) on story "
        f"{story_id or 'unknown'}; halting for human intervention"
    )


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def pm_agent(state: WorkflowState) -> dict:
    """Product Manager -- runs once, converts the requirement into a backlog."""
    update = _guard(state, "pm_agent", lambda: pm_agent_node(state))
    if _is_halt(update):
        return update
    # Handing off to technical refinement.
    update["workflow_status"] = WorkflowStatus.REFINING.value
    return update


def solution_architect_agent(state: WorkflowState) -> dict:
    """Solution Architect -- refines the whole backlog, then requests approval."""
    update = _guard(
        state, "solution_architect_agent", lambda: solution_architect_node(state)
    )
    if _is_halt(update):
        return update
    update["workflow_status"] = WorkflowStatus.AWAITING_APPROVAL.value
    return update


def human_approval_gate(state: WorkflowState) -> dict:
    """Hard human gate -- a genuine LangGraph interrupt, not a polling loop.

    The graph persists its state and suspends here until an external event
    resumes it. Resume with::

        graph.invoke(Command(resume={"status": "APPROVED",
                                     "approved_by": "someone"}), config)

    A bare ``"APPROVED"`` / ``"REJECTED"`` string is accepted too. Until this
    returns APPROVED, no repository-mutating node is reachable.
    """
    approval = state["human_approval"]

    if approval["status"] == "PENDING":
        decision = interrupt(
            {
                "kind": "human_approval",
                "message": (
                    "Approve the Solution Architect's technical design before any "
                    "repository work begins."
                ),
                "architecture_doc_ref": state["architecture_doc_ref"],
                "dev_plan_ref": state["dev_plan_ref"],
                "stories": [
                    {
                        "id": story["id"],
                        "title": story["title"],
                        "priority": story["priority"],
                        "requires_ui": story["requires_ui"],
                        "requires_api": story["requires_api"],
                        "technical_dependencies": story["technical_dependencies"],
                    }
                    for story in state["stories"]
                ],
            }
        )
        approval = _coerce_approval(decision)

    if approval["status"] == "APPROVED":
        logger.info("[human_approval_gate] APPROVED by %s", approval["approved_by"])
        return {
            "human_approval": approval,
            "workflow_status": WorkflowStatus.SELECTING_STORY.value,
        }

    if approval["status"] == "REJECTED":
        logger.info("[human_approval_gate] REJECTED -- returning to the Architect")
        return {
            "human_approval": approval,
            "workflow_status": WorkflowStatus.REFINING.value,
        }

    # Still pending: the router sends this straight back to the gate, which
    # interrupts again and waits for a real decision.
    return {
        "human_approval": approval,
        "workflow_status": WorkflowStatus.AWAITING_APPROVAL.value,
    }


def _coerce_approval(decision: object) -> ApprovalState:
    """Normalize whatever the resume signal carried into an `ApprovalState`."""
    from datetime import datetime, timezone

    if isinstance(decision, str):
        decision = {"status": decision}
    if not isinstance(decision, dict):
        return ApprovalState(
            status="PENDING", approved_by=None, approved_at=None, comments=None
        )

    status = str(decision.get("status", "")).strip().upper()
    if status not in {"APPROVED", "REJECTED", "PENDING"}:
        status = "PENDING"

    approved_at = decision.get("approved_at")
    if status in {"APPROVED", "REJECTED"} and not approved_at:
        approved_at = datetime.now(timezone.utc).isoformat()

    return ApprovalState(
        status=status,  # type: ignore[typeddict-item]
        approved_by=decision.get("approved_by"),
        approved_at=approved_at,
        comments=decision.get("comments"),
    )


def select_ready_story(state: WorkflowState) -> dict:
    """Backlog scan. A newly selected story starts its counters from zero."""
    update = _guard(
        state, "select_ready_story", lambda: select_ready_story_node(state)
    )
    if _is_halt(update):
        return update

    if update.get("current_story_id") is None:
        update["workflow_status"] = WorkflowStatus.BACKLOG_COMPLETE.value
        return update

    # Counter reset rules (schema doc section 4): `dev_attempts` and
    # `qa_iterations` reset whenever `current_story_id` changes.
    # `review_iterations` resets per PR, and the new story has no PR yet.
    update["dev_attempts"] = 0
    update["qa_iterations"] = 0
    update["review_iterations"] = 0
    update["workflow_status"] = WorkflowStatus.DEVELOPING.value
    return update


def developer_agent(state: WorkflowState) -> dict:
    """Developer -- one implementation attempt; this wrapper counts the failures.

    `MAX_DEV_ATTEMPTS` is compared here, never inside the agent: the agent
    reports whether its own self-validation passed, and the graph decides whether
    another attempt is allowed.
    """
    update = _guard(state, "developer_agent", lambda: developer_node(state))
    if _is_halt(update):
        return update

    stories = update.get("stories", state["stories"])
    story_id = update.get("current_story_id", state["current_story_id"])
    story = next((s for s in stories if s["id"] == story_id), None)
    pr_opened = story is not None and story["status"] == "IN_REVIEW"

    if pr_opened:
        new_pr = update.get("pr")
        previous_pr = state["pr"]
        # `review_iterations` resets per PR: a brand-new PR (the fresh FEATURE PR,
        # or a QA-fix FIX PR) starts a fresh review budget, while the "update the
        # SAME PR" cycle carries its count across -- that carry-over is exactly
        # what MAX_REVIEW_ITERATIONS bounds.
        if new_pr is not None and (previous_pr is None or new_pr["id"] != previous_pr["id"]):
            update["review_iterations"] = 0
        update["workflow_status"] = WorkflowStatus.REVIEWING.value
        return update

    attempts = state["dev_attempts"] + 1
    update["dev_attempts"] = attempts
    limit = state["limits"]["MAX_DEV_ATTEMPTS"]

    if attempts >= limit:
        update.update(
            _halt(
                state,
                "developer_agent",
                _limit_breach_reason("dev_attempts", attempts, limit, story_id),
            )
        )
        return update

    logger.info(
        "[developer_agent] self-validation failed (%d/%d) -- retrying locally",
        attempts,
        limit,
    )
    update["workflow_status"] = WorkflowStatus.DEVELOPING.value
    return update


def code_reviewer_agent(state: WorkflowState) -> dict:
    """Code Reviewer -- PASS merges; FAIL returns to the Developer, same PR."""
    update = _guard(
        state, "code_reviewer_agent", lambda: code_reviewer_node(state)
    )
    if _is_halt(update):
        return update

    if update.get("review_status") == "PASS":
        update["workflow_status"] = WorkflowStatus.QA_TESTING.value
        return update

    iterations = state["review_iterations"] + 1
    update["review_iterations"] = iterations
    limit = state["limits"]["MAX_REVIEW_ITERATIONS"]

    if iterations >= limit:
        update.update(
            _halt(
                state,
                "code_reviewer_agent",
                _limit_breach_reason(
                    "review_iterations", iterations, limit, state["current_story_id"]
                ),
            )
        )
        return update

    logger.info(
        "[code_reviewer_agent] FAIL (%d/%d) -- back to the Developer on the same PR",
        iterations,
        limit,
    )
    update["workflow_status"] = WorkflowStatus.DEVELOPING.value
    return update


def qa_agent(state: WorkflowState) -> dict:
    """QA -- PASS continues the backlog loop; FAIL returns to the Developer."""
    update = _guard(state, "qa_agent", lambda: qa_node(state))
    if _is_halt(update):
        return update

    if update.get("qa_status") == "PASS":
        update["workflow_status"] = WorkflowStatus.SELECTING_STORY.value
        return update

    iterations = state["qa_iterations"] + 1
    update["qa_iterations"] = iterations
    limit = state["limits"]["MAX_QA_ITERATIONS"]

    if iterations >= limit:
        # The Jira Bug is still filed (the agent did that above) before halting --
        # every QA failure must leave a linked defect behind for traceability.
        update.update(
            _halt(
                state,
                "qa_agent",
                _limit_breach_reason(
                    "qa_iterations", iterations, limit, state["current_story_id"]
                ),
            )
        )
        return update

    logger.info(
        "[qa_agent] FAIL (%d/%d) -- back to the Developer for a new fix branch",
        iterations,
        limit,
    )
    update["workflow_status"] = WorkflowStatus.DEVELOPING.value
    return update


# ---------------------------------------------------------------------------
# Conditional edges (section 2)
# ---------------------------------------------------------------------------


def _halted(state: WorkflowState) -> bool:
    return state["workflow_status"] == WorkflowStatus.HALTED


def route_after_planning(state: WorkflowState) -> str:
    """`pm_agent` -> `solution_architect_agent`, unless the run has halted.

    Section 2 draws this edge as static, and on the happy path it is. But a
    TOOL_FAILURE inside the PM node must still terminate per section 3 -- with a
    static edge it would fall through to the Architect, which would then be
    handed an empty backlog and raise a misleading validation error on top of
    the real fault.
    """
    return END if _halted(state) else "solution_architect_agent"


def route_after_refinement(state: WorkflowState) -> str:
    """`solution_architect_agent` -> `human_approval_gate`, unless halted."""
    return END if _halted(state) else "human_approval_gate"


def route_after_approval(state: WorkflowState) -> str:
    if _halted(state):
        return END
    status = state["human_approval"]["status"]
    if status == "APPROVED":
        return "select_ready_story"
    if status == "REJECTED":
        return "solution_architect_agent"
    # PENDING -- interrupt again and keep waiting.
    return "human_approval_gate"


def route_after_selection(state: WorkflowState) -> str:
    if _halted(state):
        return END
    # No READY story left: BACKLOG_COMPLETE -> END.
    return "developer_agent" if state["current_story_id"] else END


def route_after_developer(state: WorkflowState) -> str:
    if _halted(state):
        return END
    # REVIEWING means a PR is up; anything else is a self-validation retry that
    # the wrapper has already confirmed is within MAX_DEV_ATTEMPTS.
    if state["workflow_status"] == WorkflowStatus.REVIEWING:
        return "code_reviewer_agent"
    return "developer_agent"


def route_after_review(state: WorkflowState) -> str:
    if _halted(state):
        return END
    if state["review_status"] == "PASS":
        return "qa_agent"
    # FAIL within MAX_REVIEW_ITERATIONS: same story, same branch, same PR.
    return "developer_agent"


def route_after_qa(state: WorkflowState) -> str:
    if _halted(state):
        return END
    if state["qa_status"] == "PASS":
        # Remaining READY stories? -- re-scan the backlog to find out.
        return "select_ready_story"
    # FAIL within MAX_QA_ITERATIONS: new fix branch, new PR, full re-review.
    return "developer_agent"


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def build_graph(checkpointer: Checkpointer | None = None):
    """Compile and return the LangGraph `StateGraph` over `WorkflowState`.

    Args:
        checkpointer: persistence backend. `human_approval_gate` uses a real
            LangGraph interrupt, which requires one; an `InMemorySaver` is used
            by default so the graph is runnable out of the box. Swap in a durable
            checkpointer for anything that must survive the process.

    Invoke with a config carrying a thread id, and raise the recursion limit --
    the backlog loop takes many super-steps::

        graph.invoke(state, {"configurable": {"thread_id": "run-1"},
                             "recursion_limit": DEFAULT_RECURSION_LIMIT})
    """
    builder = StateGraph(WorkflowState)

    builder.add_node("pm_agent", pm_agent)
    builder.add_node("solution_architect_agent", solution_architect_agent)
    builder.add_node("human_approval_gate", human_approval_gate)
    builder.add_node("select_ready_story", select_ready_story)
    builder.add_node("developer_agent", developer_agent)
    builder.add_node("code_reviewer_agent", code_reviewer_agent)
    builder.add_node("qa_agent", qa_agent)

    # START -> pm_agent -> solution_architect_agent -> human_approval_gate
    # The two hand-offs are conditional only so a TOOL_FAILURE halt can reach
    # END; on the happy path they are the static edges section 2 draws.
    builder.add_edge(START, "pm_agent")
    builder.add_conditional_edges(
        "pm_agent",
        route_after_planning,
        {"solution_architect_agent": "solution_architect_agent", END: END},
    )
    builder.add_conditional_edges(
        "solution_architect_agent",
        route_after_refinement,
        {"human_approval_gate": "human_approval_gate", END: END},
    )

    builder.add_conditional_edges(
        "human_approval_gate",
        route_after_approval,
        {
            "select_ready_story": "select_ready_story",
            "solution_architect_agent": "solution_architect_agent",
            "human_approval_gate": "human_approval_gate",
            END: END,
        },
    )
    builder.add_conditional_edges(
        "select_ready_story",
        route_after_selection,
        {"developer_agent": "developer_agent", END: END},
    )
    builder.add_conditional_edges(
        "developer_agent",
        route_after_developer,
        {
            "code_reviewer_agent": "code_reviewer_agent",
            "developer_agent": "developer_agent",
            END: END,
        },
    )
    builder.add_conditional_edges(
        "code_reviewer_agent",
        route_after_review,
        {"qa_agent": "qa_agent", "developer_agent": "developer_agent", END: END},
    )
    builder.add_conditional_edges(
        "qa_agent",
        route_after_qa,
        {
            "select_ready_story": "select_ready_story",
            "developer_agent": "developer_agent",
            END: END,
        },
    )

    return builder.compile(checkpointer=checkpointer or InMemorySaver())
