"""LangGraph shared state schema.

Implements `docs/agents/00-shared-state-schema.md` verbatim: this is the single
state object every agent reads from and writes to. Agents share no conversational
memory -- all cross-agent context flows through `WorkflowState`.

Jira and GitHub remain the authoritative sources of truth. This state stores
**pointers only** (IDs, URLs, statuses, counters); no payload is duplicated here,
so every agent re-reads live data from its tool of record before acting.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, TypedDict

__all__ = [
    "WorkflowState",
    "EpicRef",
    "StoryRef",
    "StoryStatus",
    "ApprovalState",
    "ApprovalStatus",
    "RepoRef",
    "PullRequestRef",
    "PullRequestKind",
    "PullRequestStatus",
    "Finding",
    "FindingSource",
    "FindingSeverity",
    "RetryLimits",
    "WorkflowStatus",
    "ErrorEntry",
    "ErrorKind",
    "ReviewStatus",
    "QAStatus",
    "QATestType",
    "DEFAULT_LIMITS",
    "BLOCKING_SEVERITIES",
    "new_error_entry",
    "create_initial_state",
]


# ---------------------------------------------------------------------------
# Literal aliases -- named once so every agent references them identically.
# ---------------------------------------------------------------------------

StoryStatus = Literal[
    "BACKLOG",      # created by PM, not yet technically refined
    "REFINED",      # technically refined by Architect, backlog not yet approved
    "READY",        # approved + all dependencies satisfied, unclaimed
    "IN_PROGRESS",  # Developer actively implementing
    "IN_REVIEW",    # PR open, Code Reviewer evaluating
    "IN_QA",        # merged, QA evaluating
    "DONE",         # QA PASS, no open bug
    "BLOCKED",      # unmet dependency or unresolved failure
]

ApprovalStatus = Literal["PENDING", "APPROVED", "REJECTED"]

PullRequestKind = Literal["FEATURE", "FIX"]  # FIX = QA-defect fix branch/PR
PullRequestStatus = Literal["OPEN", "MERGED", "CLOSED"]

FindingSource = Literal["CODE_REVIEWER", "SONARQUBE"]
FindingSeverity = Literal["BLOCKER", "MAJOR", "MINOR", "INFO"]

ReviewStatus = Literal["PENDING", "PASS", "FAIL"]
QAStatus = Literal["PENDING", "PASS", "FAIL"]
QATestType = Literal["API", "UI", "REGRESSION"]

ErrorKind = Literal["VALIDATION_FAILURE", "TOOL_FAILURE", "HARD_LIMIT"]

#: A single finding at one of these severities is sufficient for a review FAIL
#: (docs/agents/04-code-reviewer-agent.md section 6). MINOR/INFO are reported,
#: but do not gate the merge.
BLOCKING_SEVERITIES: frozenset[str] = frozenset({"BLOCKER", "MAJOR"})


# ---------------------------------------------------------------------------
# Supporting types (schema doc section 2)
# ---------------------------------------------------------------------------


class EpicRef(TypedDict):
    """Pointer to a Jira Epic created by the PM Agent."""

    id: str     # Jira Epic key, e.g. "PROJ-1"
    title: str


class StoryRef(TypedDict):
    """Pointer to a Jira Story.

    Business fields (`title`, `priority`, `business_dependencies`) are written by
    the PM Agent and are read-only to every later agent. Technical fields
    (`technical_dependencies`, `requires_ui`, `requires_api`) are written once by
    the Solution Architect and are read-only downstream.

    Note: `description` and `acceptance_criteria` deliberately do **not** live
    here. Jira is authoritative for them -- the QA Agent re-reads the original
    acceptance criteria from Jira rather than trusting a copy carried in state.
    """

    id: str        # Jira Story key, e.g. "PROJ-14"
    epic_id: str
    title: str
    priority: int  # lower number = higher priority
    business_dependencies: list[str]   # Story IDs (set by PM)
    technical_dependencies: list[str]  # Story IDs (set by Solution Architect)
    requires_ui: bool   # adaptive scope flag, set by Solution Architect
    requires_api: bool
    status: StoryStatus


class ApprovalState(TypedDict):
    """Human-in-the-loop gate state (orchestration-owned; no agent may grant it)."""

    status: ApprovalStatus
    approved_by: str | None
    approved_at: str | None   # ISO-8601 UTC
    comments: str | None      # rejection feedback, read by the Architect on re-entry


class RepoRef(TypedDict):
    owner: str
    name: str
    default_branch: str


class PullRequestRef(TypedDict):
    id: str
    url: str
    story_id: str
    kind: PullRequestKind
    status: PullRequestStatus


class Finding(TypedDict):
    """One reviewable defect, from manual review or SonarQube.

    Must reference a specific file/line so the Developer can act without
    re-deriving context (docs/agents/04-code-reviewer-agent.md section 6).
    """

    source: FindingSource
    severity: FindingSeverity
    file: str
    line: int | None
    message: str


class RetryLimits(TypedDict):
    """Hard limits. Config only -- never mutated at runtime."""

    MAX_DEV_ATTEMPTS: int
    MAX_REVIEW_ITERATIONS: int
    MAX_QA_ITERATIONS: int
    MAX_TOOL_RETRIES: int


class WorkflowStatus(str, Enum):
    """Orchestration phase.

    A `str` Enum, so `state["workflow_status"] == WorkflowStatus.HALTED` holds
    whether the field carries the member or its plain-string value. State writes
    store `.value`: LangGraph's checkpointer will not deserialize unregistered
    types, so a bare string is what survives a persisted run.
    """

    PLANNING = "PLANNING"                    # PM Agent running
    REFINING = "REFINING"                    # Solution Architect running
    AWAITING_APPROVAL = "AWAITING_APPROVAL"  # paused at human gate
    SELECTING_STORY = "SELECTING_STORY"      # Developer scanning backlog
    DEVELOPING = "DEVELOPING"                # Developer implementing current story
    REVIEWING = "REVIEWING"                  # Code Reviewer running
    QA_TESTING = "QA_TESTING"                # QA running
    BACKLOG_COMPLETE = "BACKLOG_COMPLETE"    # no READY stories remain -> END
    HALTED = "HALTED"                        # hard limit hit / unrecoverable error


class ErrorEntry(TypedDict):
    timestamp: str   # ISO-8601 UTC
    agent: str
    kind: ErrorKind
    detail: str


# ---------------------------------------------------------------------------
# Top-level state object (schema doc section 1)
# ---------------------------------------------------------------------------


class WorkflowState(TypedDict):
    """The single LangGraph state dictionary shared by every node."""

    # --- Origin ---
    requirement: str  # original natural-language business requirement

    # --- Backlog (pointers into Jira; Jira is authoritative) ---
    epics: list[EpicRef]
    stories: list[StoryRef]

    # --- Technical refinement output (Solution Architect) ---
    architecture_doc_ref: str | None  # Jira/Confluence attachment or page ID
    dev_plan_ref: str | None

    # --- Human-in-the-loop gate ---
    human_approval: ApprovalState

    # --- Active story being implemented ---
    current_story_id: str | None

    # --- Repo / branch / PR pointers (current story) ---
    repo: RepoRef
    branch_name: str | None
    pr: PullRequestRef | None

    # --- Code Review ---
    review_status: ReviewStatus | None
    review_findings: list[Finding]
    sonarqube_report_ref: str | None

    # --- QA ---
    qa_status: QAStatus | None
    qa_test_type: QATestType | None
    qa_results_ref: str | None
    jira_bug_id: str | None

    # --- Retry / iteration counters (reset per story where noted, section 4) ---
    dev_attempts: int        # self-validation retries, resets per story
    review_iterations: int   # code-review round-trips, resets per PR
    qa_iterations: int       # QA fail -> fix-branch round-trips, resets per story
    tool_retries: int        # transient tool-call retries, resets per tool call

    # --- Hard limits (config, not mutated at runtime) ---
    limits: RetryLimits

    # --- Orchestration status ---
    workflow_status: WorkflowStatus
    halt_reason: str | None
    error_log: list[ErrorEntry]


# ---------------------------------------------------------------------------
# Defaults & constructors
# ---------------------------------------------------------------------------

#: docs/agents/00-orchestration-and-routing.md section 3. `config.settings` may
#: override these from the environment; this literal is the fallback of record.
DEFAULT_LIMITS: RetryLimits = RetryLimits(
    MAX_DEV_ATTEMPTS=3,
    MAX_REVIEW_ITERATIONS=3,
    MAX_QA_ITERATIONS=2,
    MAX_TOOL_RETRIES=3,
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_error_entry(agent: str, kind: ErrorKind, detail: str) -> ErrorEntry:
    """Build a timestamped `ErrorEntry` for appending to `error_log`."""
    return ErrorEntry(
        timestamp=_utcnow_iso(),
        agent=agent,
        kind=kind,
        detail=detail,
    )


def create_initial_state(
    requirement: str,
    repo: RepoRef,
    limits: RetryLimits | None = None,
) -> WorkflowState:
    """Build the state the graph is invoked with at `START`.

    Only `requirement` and `repo` are populated at entry; everything downstream is
    written by the agent that owns it (see the field-ownership matrix, section 3).
    """
    return WorkflowState(
        requirement=requirement,
        epics=[],
        stories=[],
        architecture_doc_ref=None,
        dev_plan_ref=None,
        human_approval=ApprovalState(
            status="PENDING",
            approved_by=None,
            approved_at=None,
            comments=None,
        ),
        current_story_id=None,
        repo=repo,
        branch_name=None,
        pr=None,
        review_status=None,
        review_findings=[],
        sonarqube_report_ref=None,
        qa_status=None,
        qa_test_type=None,
        qa_results_ref=None,
        jira_bug_id=None,
        dev_attempts=0,
        review_iterations=0,
        qa_iterations=0,
        tool_retries=0,
        limits=RetryLimits(**DEFAULT_LIMITS) if limits is None else limits,
        workflow_status=WorkflowStatus.PLANNING.value,
        halt_reason=None,
        error_log=[],
    )
