# Autonomous Software Engineering Team — Agent Specification Index

This folder contains the implementation-ready specifications for the multi-agent system
described in `Autonomous Software Engineering Team — Technical Specification`. It is
orchestrated via **LangGraph**, uses **LangChain** for agent/LLM logic, and integrates with
**Jira, GitHub, SonarQube, and Playwright** as the tools of record.

The system converts a natural-language business requirement into a technically refined,
human-approved Jira backlog, then iteratively implements, reviews, and QA-validates each
backlog story until no "READY" stories remain.

## Reading order

1. [00-shared-state-schema.md](00-shared-state-schema.md) — the single LangGraph state
   dictionary every agent reads from and writes to. Read this first; every agent file
   references its field names verbatim.
2. [00-orchestration-and-routing.md](00-orchestration-and-routing.md) — the LangGraph graph
   definition: nodes, conditional edges, retry counters, and hard limits. This is the
   workflow-level routing logic that agents must NOT implement themselves (per the
   Decision Principle: agents make local decisions, LangGraph makes routing decisions).
3. [01-product-manager-agent.md](01-product-manager-agent.md)
4. [02-solution-architect-agent.md](02-solution-architect-agent.md)
5. [03-developer-agent.md](03-developer-agent.md)
6. [04-code-reviewer-agent.md](04-code-reviewer-agent.md)
7. [05-qa-agent.md](05-qa-agent.md)

## Canonical workflow (source of truth for all specs below)

```
Business Requirement
   -> PM Agent -> creates JIRA Epics/Stories/Acceptance Criteria/Priority/Dependencies
   -> Solution Architect Agent -> Technically refined stories + Architecture + API + DB Design + Dev Plan
   -> Human Approval (hard gate)
   -> Developer Agent
        -> Select highest-priority READY story
        -> Create GitHub feature branch
        -> Generate/Modify code
        -> Run application and unit tests
        -> Developer self-validation
        -> Open GitHub Pull Request
   -> Code Reviewer Agent
        -> Code Review + SonarQube analysis
        -> PASS? --NO--> Review Findings/Errors -> Developer Agent -> Update SAME PR --(loop)--> Code Reviewer Agent
        -> PASS? --YES-> PR Merge -> QA Agent
   -> QA Agent
        -> API/UI/Regression test (adaptive to architecture)
        -> FAIL --> Jira Bug -> Developer Agent -> NEW fix branch -> Implement fix -> NEW GitHub PR --(loop)--> Code Reviewer Agent
        -> PASS --> Remaining "READY" stories?
              -> YES -> Developer Agent (selects next story, loop restarts at "Select highest-priority READY story")
              -> NO  -> END
```

Every agent spec in this folder implements exactly one box (or cluster of boxes) in this
diagram — no agent performs work belonging to another agent's box, and no agent decides
routing between boxes (that is LangGraph's job, see `00-orchestration-and-routing.md`).

## Non-negotiable system boundaries (apply across all agents)

- **No autonomous deployment.** No agent may deploy to production or manage infrastructure.
- **Approval gate.** The Developer Agent is blocked from any repository write until a human
  approves the Solution Architect's technical design.
- **Merge control.** The Developer Agent never merges its own PR. Only a PASS from the Code
  Reviewer Agent results in a merge.
- **Forced quality gates.** Code Review and QA cannot be bypassed, and retries are bounded by
  hard limits (`MAX_DEV_ATTEMPTS`, `MAX_REVIEW_ITERATIONS`, `MAX_QA_ITERATIONS`,
  `MAX_TOOL_RETRIES`). On exhaustion, the workflow halts safely for human intervention rather
  than looping indefinitely.
- **Sources of truth.** Jira and GitHub hold the real data. The shared state stores pointers
  (IDs, URLs, statuses, counters) only — never duplicated payloads.
- **Structured outputs only.** Every agent returns a typed JSON object (never free-form prose)
  so LangGraph can deterministically route on it.

## Project Structure & Configuration

To ensure correct scaffolding, the repository must follow this modular layout:

autonomous-ai-team/
├── agents/
│   ├── product_manager.py
│   ├── solution_architect.py
│   ├── developer.py
│   ├── code_reviewer.py
│   └── qa_agent.py
├── core/
│   ├── state.py         # LangGraph TypedDict state schema
│   ├── graph.py         # LangGraph nodes and conditional routing edges
│   └── prompts.py       # Centralized system prompts
├── tools/
│   ├── github_client.py
│   ├── jira_client.py
│   └── testing_tools.py # SonarQube & Playwright hooks
├── config/
│   └── settings.py      # Environment variable loading and retry limits
├── requirements.txt
└── main.py              # Entry point to initialize and invoke the graph

**Required Environment Variables (.env)**
* `LLM_API_KEY`: Credentials for the primary reasoning model.
* `GITHUB_TOKEN`: For repository cloning, branch creation, and PR management.
* `JIRA_API_TOKEN` & `JIRA_BASE_URL`: For reading the backlog and creating bugs.
* `SONAR_TOKEN`: For triggering and reading static analysis results.
* `MAX_DEV_ATTEMPTS`: Limit for self-validation correction loops.
* `MAX_REVIEW_ITERATIONS`: Limit for PR review correction loops.
* `MAX_QA_ITERATIONS`: Limit for QA defect resolution loops.