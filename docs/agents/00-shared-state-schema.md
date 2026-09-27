# Shared Workflow State Schema

> Source: PDF §6 "Shared Workflow State & Data Flow", §9 "Error Handling & Retry Mechanisms".

Agents do not share conversational memory. All cross-agent context flows through a single
LangGraph state object (a `TypedDict` / Pydantic model). Jira and GitHub remain the
**authoritative source of truth**; this state stores **pointers** (IDs, URLs, statuses, retry
counters) so the payload stays small and every agent re-reads live data from the tool of
record before acting.

## 1. Top-level state object

```python
class WorkflowState(TypedDict):
    # --- Origin ---
    requirement: str                       # original natural-language business requirement

    # --- Backlog (pointers into Jira; Jira is authoritative) ---
    epics: list[EpicRef]
    stories: list[StoryRef]

    # --- Technical refinement output (Solution Architect) ---
    architecture_doc_ref: str | None        # Jira/Confluence attachment or page ID
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
    review_status: Literal["PENDING", "PASS", "FAIL"] | None
    review_findings: list[Finding]
    sonarqube_report_ref: str | None

    # --- QA ---
    qa_status: Literal["PENDING", "PASS", "FAIL"] | None
    qa_test_type: Literal["API", "UI", "REGRESSION"] | None
    qa_results_ref: str | None
    jira_bug_id: str | None

    # --- Retry / iteration counters (reset per story where noted) ---
    dev_attempts: int                       # self-validation retries, resets per story
    review_iterations: int                  # code-review round-trips, resets per PR
    qa_iterations: int                      # QA fail -> fix-branch round-trips, resets per story
    tool_retries: int                       # transient tool-call retries, resets per tool call

    # --- Hard limits (config, not mutated at runtime) ---
    limits: RetryLimits

    # --- Orchestration status ---
    workflow_status: WorkflowStatus
    halt_reason: str | None
    error_log: list[ErrorEntry]
```

## 2. Supporting types

```python
class EpicRef(TypedDict):
    id: str                 # Jira Epic key, e.g. "PROJ-1"
    title: str

class StoryRef(TypedDict):
    id: str                 # Jira Story key, e.g. "PROJ-14"
    epic_id: str
    title: str
    priority: int            # lower number = higher priority (or enum P0/P1/P2)
    business_dependencies: list[str]   # Story IDs (set by PM)
    technical_dependencies: list[str]  # Story IDs (set by Solution Architect)
    requires_ui: bool         # adaptive scope flag, set by Solution Architect
    requires_api: bool
    status: Literal[
        "BACKLOG",        # created by PM, not yet technically refined
        "REFINED",        # technically refined by Architect, backlog not yet approved
        "READY",          # approved + all dependencies satisfied, unclaimed
        "IN_PROGRESS",    # Developer actively implementing
        "IN_REVIEW",      # PR open, Code Reviewer evaluating
        "IN_QA",          # merged, QA evaluating
        "DONE",           # QA PASS, no open bug
        "BLOCKED",        # unmet dependency or unresolved failure
    ]

class ApprovalState(TypedDict):
    status: Literal["PENDING", "APPROVED", "REJECTED"]
    approved_by: str | None
    approved_at: str | None
    comments: str | None

class RepoRef(TypedDict):
    owner: str
    name: str
    default_branch: str

class PullRequestRef(TypedDict):
    id: str
    url: str
    story_id: str
    kind: Literal["FEATURE", "FIX"]   # FIX = QA-defect fix branch/PR
    status: Literal["OPEN", "MERGED", "CLOSED"]

class Finding(TypedDict):
    source: Literal["CODE_REVIEWER", "SONARQUBE"]
    severity: Literal["BLOCKER", "MAJOR", "MINOR", "INFO"]
    file: str
    line: int | None
    message: str

class RetryLimits(TypedDict):
    MAX_DEV_ATTEMPTS: int
    MAX_REVIEW_ITERATIONS: int
    MAX_QA_ITERATIONS: int
    MAX_TOOL_RETRIES: int

class WorkflowStatus(str, Enum):
    PLANNING = "PLANNING"                     # PM Agent running
    REFINING = "REFINING"                     # Solution Architect running
    AWAITING_APPROVAL = "AWAITING_APPROVAL"   # paused at human gate
    SELECTING_STORY = "SELECTING_STORY"       # Developer scanning backlog
    DEVELOPING = "DEVELOPING"                 # Developer implementing current story
    REVIEWING = "REVIEWING"                   # Code Reviewer running
    QA_TESTING = "QA_TESTING"                 # QA running
    BACKLOG_COMPLETE = "BACKLOG_COMPLETE"     # no READY stories remain -> END
    HALTED = "HALTED"                         # hard limit hit / unrecoverable error

class ErrorEntry(TypedDict):
    timestamp: str
    agent: str
    kind: Literal["VALIDATION_FAILURE", "TOOL_FAILURE", "HARD_LIMIT"]
    detail: str
```

## 3. Field ownership matrix

| Field | Written by | Read by |
|---|---|---|
| `requirement` | user / entrypoint | PM |
| `epics`, `stories` (business fields) | PM | Solution Architect, Developer |
| `stories[].technical_dependencies`, `requires_ui`, `requires_api`, status `REFINED` | Solution Architect | Developer, QA |
| `architecture_doc_ref`, `dev_plan_ref` | Solution Architect | Developer, Code Reviewer |
| `human_approval` | Human (via orchestration pause) | LangGraph router |
| `current_story_id`, `stories[].status` transitions | Developer, LangGraph | all downstream agents |
| `branch_name`, `pr` | Developer | Code Reviewer, QA |
| `review_status`, `review_findings`, `sonarqube_report_ref` | Code Reviewer | LangGraph router, Developer |
| `qa_status`, `qa_results_ref`, `jira_bug_id` | QA | LangGraph router, Developer |
| `dev_attempts`, `review_iterations`, `qa_iterations`, `tool_retries` | LangGraph (incremented on each routed retry) | LangGraph router (compared against `limits`) |
| `workflow_status`, `halt_reason`, `error_log` | LangGraph | all agents (context), human operator |

## 4. Counter reset rules (see §9 of PDF)

- `dev_attempts` resets to 0 whenever `current_story_id` changes.
- `review_iterations` resets to 0 whenever a **new** PR is opened (`pr.kind == FEATURE`), but
  **carries across** "Update Same PR" cycles for the current PR — it is exactly the counter that
  enforces `MAX_REVIEW_ITERATIONS` on the "Review Findings/Errors -> Developer -> Update Same PR"
  loop in the diagram.
- `qa_iterations` resets to 0 whenever `current_story_id` changes; it increments each time QA
  FAILs and a new fix branch/PR is created for that story.
- `tool_retries` resets to 0 at the start of every individual tool invocation.
