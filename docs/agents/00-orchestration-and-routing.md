# LangGraph Orchestration & Routing

> Source: PDF §3 "Canonical Workflow", §4 "System Architecture & Orchestration", §6
> "State-Driven Routing", §7 "Agent Execution Model", §9 "Error Handling & Retry Mechanisms",
> §10 "Human-in-the-Loop & Safety Controls".

This document defines what **LangGraph** owns, as distinct from what each **agent** owns.
Per the PDF's Decision Principle (§8): *agents make local decisions (is this code acceptable?);
LangGraph makes workflow-level routing decisions (where does execution go next?)*. No agent
file in this folder should re-implement anything in this document.

## 1. Graph nodes

| Node | Implements |
|---|---|
| `pm_agent` | Product Manager Agent |
| `solution_architect_agent` | Solution Architect Agent |
| `human_approval_gate` | Interrupt/pause node — not an LLM agent |
| `select_ready_story` | Developer Agent: backlog scan + selection (§6 logic) |
| `developer_agent` | Developer Agent: branch, code, self-validate, PR |
| `code_reviewer_agent` | Code Reviewer Agent |
| `qa_agent` | QA Agent |

## 2. Edges (conditional routing)

```
START -> pm_agent -> solution_architect_agent -> human_approval_gate

human_approval_gate:
    APPROVED -> select_ready_story
    REJECTED -> solution_architect_agent   # revise design, re-submit for approval
    PENDING  -> human_approval_gate         # graph interrupt; waits for external signal

select_ready_story:
    READY story found -> developer_agent
    none found         -> BACKLOG_COMPLETE -> END

developer_agent:
    self-validation FAIL and dev_attempts < MAX_DEV_ATTEMPTS -> developer_agent (retry locally)
    self-validation FAIL and dev_attempts >= MAX_DEV_ATTEMPTS -> HALTED -> END
    PR opened -> code_reviewer_agent

code_reviewer_agent:
    review_status == FAIL and review_iterations < MAX_REVIEW_ITERATIONS
        -> developer_agent (update SAME pr)
    review_status == FAIL and review_iterations >= MAX_REVIEW_ITERATIONS
        -> HALTED -> END
    review_status == PASS -> [merge PR] -> qa_agent

qa_agent:
    qa_status == FAIL and qa_iterations < MAX_QA_ITERATIONS
        -> [create Jira Bug] -> developer_agent (NEW fix branch + PR)
    qa_status == FAIL and qa_iterations >= MAX_QA_ITERATIONS
        -> HALTED -> END
    qa_status == PASS -> select_ready_story   # re-scan backlog for next READY story
```

Note the two structurally different Developer re-entries the diagram distinguishes:
- **Code Review FAIL** -> Developer updates the **same PR** (`pr.kind` unchanged, same
  `pr.id`; only `review_iterations` increments).
- **QA FAIL** -> Developer opens a **brand-new fix branch and PR** (`pr.kind = "FIX"`, new
  `pr.id`, `branch_name` changes); the new PR re-enters `code_reviewer_agent` from the top,
  exactly as the diagram shows ("NEW FIX BRANCH -> IMPLEMENT FIX -> NEW GITHUB PR" feeding back
  into "Code Reviewer Agent").

## 3. Hard limits and safe termination (§9)

```python
DEFAULT_LIMITS = RetryLimits(
    MAX_DEV_ATTEMPTS=3,
    MAX_REVIEW_ITERATIONS=3,
    MAX_QA_ITERATIONS=2,
    MAX_TOOL_RETRIES=3,
)
```

- Counters are compared **by LangGraph**, not by the agent whose loop they gate — an agent
  reports its status honestly; LangGraph decides whether another attempt is allowed.
- On any hard-limit breach: set `workflow_status = HALTED`, populate `halt_reason` with a
  human-readable explanation and the offending counter/story ID, append an `ErrorEntry`, and
  end the run. The full state (Jira/GitHub pointers, findings, results) is preserved so a
  human can resume manually — nothing is rolled back automatically.
- **Tool failures** (GitHub API timeout, Playwright execution error, Jira API error, SonarQube
  unavailable) are distinct from validation failures: they retry the *same tool call*, up to
  `MAX_TOOL_RETRIES`, with no state/story impact beyond the `tool_retries` counter. Exhausting
  tool retries is itself a `HARD_LIMIT` halt (`kind = "TOOL_FAILURE"`).

## 4. Human-in-the-loop gate (§10)

- `human_approval_gate` is a genuine LangGraph interrupt, not a polling loop: the graph
  persists state and suspends until an external event (human approval/rejection via the UI,
  CLI, or Jira workflow transition) resumes it.
- Until `human_approval.status == "APPROVED"`, the Developer Agent must never be invoked and
  no repository-mutating tool call is reachable from any node.
- The Code Reviewer Agent is the only actor that can move a PR toward merge; LangGraph never
  auto-merges on the Developer's say-so (Merge Control, §10).

## 5. Agent Execution Model mapped to LangGraph (§7)

Each agent node runs the same five-step cycle before returning control to LangGraph:

1. **Read** — load only the state fields the agent needs (see each agent file's "Inputs").
2. **Reason** — evaluate against its own responsibilities only (no cross-agent decisions).
3. **Act** — call exactly the tools listed in its "Tools" section.
4. **Observe** — capture the tool's raw result.
5. **Update** — return a structured output object; LangGraph merges it into `WorkflowState`
   and evaluates the conditional edges in §2 above.

## 6. Adaptive (requirement-dependent) architecture (§4, §11)

`stories[].requires_ui` is set once, by the Solution Architect, and is treated as read-only by
every downstream node:
- `developer_agent` only generates frontend code/tests for a story where `requires_ui == true`.
- `qa_agent` only invokes Playwright when `requires_ui == true` for the story under test;
  otherwise it runs API/functional/regression tests exclusively.
This flag is the single mechanism the diagram's "backend-only bypasses UI generation and UI
testing entirely" rule hangs off of — do not infer UI-ness from code inspection at runtime.
