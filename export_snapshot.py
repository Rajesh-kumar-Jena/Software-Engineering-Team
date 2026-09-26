"""Export a snapshot of the live system for the dashboard to render.

The frontend is a view over real data, not a mockup: this pulls the current Jira
backlog, the GitHub pull requests and the code-review findings the agents
actually posted, plus the static pipeline definition and the configured model
routing. Re-run it to refresh what the dashboard shows.

    python export_snapshot.py            # writes frontend/src/data/snapshot.json
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from requests.auth import HTTPBasicAuth

from config import settings
from tools import RestClient, ToolFailure

OUT = Path("frontend/src/data/snapshot.json")

#: The graph as `core/graph.py` builds it -- nodes and the conditional edges
#: between them. Static, because it is the architecture rather than run data.
PIPELINE = {
    "nodes": [
        {"id": "pm_agent", "label": "Product Manager", "role": "Decides WHAT to build", "tools": ["Jira"]},
        {"id": "solution_architect_agent", "label": "Solution Architect", "role": "Decides HOW to build it", "tools": ["Jira"]},
        {"id": "human_approval_gate", "label": "Human Approval", "role": "Hard gate — LangGraph interrupt", "tools": []},
        {"id": "select_ready_story", "label": "Select READY Story", "role": "Deterministic backlog scan", "tools": ["Jira"]},
        {"id": "developer_agent", "label": "Developer", "role": "Only agent that writes code", "tools": ["GitHub", "Jira"]},
        {"id": "code_reviewer_agent", "label": "Code Reviewer", "role": "Quality gate — only PASS merges", "tools": ["GitHub", "SonarQube"]},
        {"id": "qa_agent", "label": "QA", "role": "Tests against original criteria", "tools": ["Playwright", "Jira"]},
    ],
    "edges": [
        {"from": "START", "to": "pm_agent", "label": ""},
        {"from": "pm_agent", "to": "solution_architect_agent", "label": ""},
        {"from": "solution_architect_agent", "to": "human_approval_gate", "label": ""},
        {"from": "human_approval_gate", "to": "select_ready_story", "label": "APPROVED"},
        {"from": "human_approval_gate", "to": "solution_architect_agent", "label": "REJECTED"},
        {"from": "select_ready_story", "to": "developer_agent", "label": "story found"},
        {"from": "select_ready_story", "to": "END", "label": "none left"},
        {"from": "developer_agent", "to": "code_reviewer_agent", "label": "PR opened"},
        {"from": "developer_agent", "to": "developer_agent", "label": "self-validation FAIL"},
        {"from": "code_reviewer_agent", "to": "developer_agent", "label": "FAIL — same PR"},
        {"from": "code_reviewer_agent", "to": "qa_agent", "label": "PASS — merge"},
        {"from": "qa_agent", "to": "developer_agent", "label": "FAIL — new fix PR"},
        {"from": "qa_agent", "to": "select_ready_story", "label": "PASS"},
    ],
}

#: Safety mechanisms, and what each one was observed catching in live runs.
SAFEGUARDS = [
    {"name": "MAX_DEV_ATTEMPTS", "limit": settings.LIMITS["MAX_DEV_ATTEMPTS"], "guards": "Self-validation retries", "observed": "Halted a story whose generated code would not parse"},
    {"name": "MAX_REVIEW_ITERATIONS", "limit": settings.LIMITS["MAX_REVIEW_ITERATIONS"], "guards": "Code-review round trips on one PR", "observed": "Halted when the Developer could not clear the reviewer"},
    {"name": "MAX_QA_ITERATIONS", "limit": settings.LIMITS["MAX_QA_ITERATIONS"], "guards": "QA defect fix branches", "observed": "Bounded the QA-fix loop; a Jira bug is filed every failure"},
    {"name": "MAX_TOOL_RETRIES", "limit": settings.LIMITS["MAX_TOOL_RETRIES"], "guards": "Transient tool failures", "observed": "Retried GitHub/Jira calls; exhaustion halts as TOOL_FAILURE"},
    {"name": "LLM_TIMEOUT", "limit": int(settings.LLM_TIMEOUT), "guards": "A stalled model call", "observed": "Caught a generation that ran 22 minutes without returning"},
]


def _log_timeline(path: Path) -> list[dict]:
    """Parse the most recent run log into an ordered stage timeline."""
    if not path.is_file():
        return []
    pattern = re.compile(
        r"\[(pm_agent|solution_architect_agent|human_approval_gate|select_ready_story|"
        r"developer_agent|code_reviewer_agent|qa_agent)\]\s*(.*)"
    )
    events: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.search(line)
        if not match:
            continue
        node, detail = match.group(1), match.group(2).strip()
        level = "error" if "HALTED" in line else "warn" if line.startswith("WARNING") else "info"
        events.append({"node": node, "detail": detail[:200], "level": level})
    return events[:400]


def build_snapshot() -> dict:
    """Read the project as it stands right now: backlog, pull requests, findings.

    Shared by the CLI exporter and the dashboard's /api/snapshot endpoint, so
    the page reflects the current Jira and GitHub state rather than whatever was
    true when the frontend was last built. Each source degrades independently:
    an unreachable Jira leaves the backlog empty instead of failing the page.
    """
    snapshot: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pipeline": PIPELINE,
        "safeguards": SAFEGUARDS,
        "models": [
            {"agent": a, "provider": p, "model": m}
            for a, p, m in settings.describe_agent_models()
        ],
        "project": {
            "jira_project": settings.JIRA_PROJECT_KEY,
            "jira_url": settings.JIRA_BASE_URL,
            "repo": f"{settings.GITHUB_REPO_OWNER}/{settings.GITHUB_REPO_NAME}",
        },
        "epics": [],
        "stories": [],
        "pull_requests": [],
        "findings": [],
        "timeline": _log_timeline(Path("coupon-run.log")),
    }

    # --- Jira ---------------------------------------------------------------
    try:
        jira = RestClient(
            "jira",
            f"{settings.JIRA_BASE_URL.rstrip('/')}/rest/api/3",
            auth=HTTPBasicAuth(settings.JIRA_USER_EMAIL, settings.JIRA_API_TOKEN),
        )
        issues = jira.get_json(
            "/search/jql",
            params={
                "jql": f"project={settings.JIRA_PROJECT_KEY} ORDER BY key",
                "maxResults": 100,
                "fields": "summary,status,issuetype,priority,description",
            },
        ).get("issues", [])
        for issue in issues:
            fields = issue["fields"]
            row = {
                "key": issue["key"],
                "summary": fields.get("summary", ""),
                "status": (fields.get("status") or {}).get("name", ""),
                "type": (fields.get("issuetype") or {}).get("name", ""),
            }
            if row["type"].lower() == "epic":
                snapshot["epics"].append(row)
            else:
                snapshot["stories"].append(row)
    except ToolFailure as exc:
        print(f"warning: could not read Jira ({exc})", file=sys.stderr)

    # --- GitHub -------------------------------------------------------------
    try:
        gh = RestClient(
            "github",
            settings.GITHUB_API_URL.rstrip("/"),
            headers={
                "Authorization": f"Bearer {settings.GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
        )
        base = f"/repos/{settings.GITHUB_REPO_OWNER}/{settings.GITHUB_REPO_NAME}"
        for pull in gh.get_json(base + "/pulls", params={"state": "all", "per_page": 30}):
            number = pull["number"]
            snapshot["pull_requests"].append(
                {
                    "number": number,
                    "title": pull["title"],
                    "branch": pull["head"]["ref"],
                    "state": "merged" if pull.get("merged_at") else pull["state"],
                    "url": pull["html_url"],
                    "story": pull["title"].split(":")[0].strip(),
                }
            )
            # The findings the Code Reviewer actually posted on this PR.
            for comment in gh.get_json(f"{base}/issues/{number}/comments"):
                body = comment.get("body") or ""
                if not body.startswith("### Code review"):
                    continue
                for line in body.splitlines():
                    match = re.match(
                        r"- \*\*(\w+)\*\* \((\w+)\) `([^`]+)`(?: line (\d+))? — (.*)", line
                    )
                    if not match:
                        continue
                    message = match.group(5)[:300].strip()
                    # Drop schema placeholders. Early runs used a weaker
                    # reviewer that echoed the example's literal "string";
                    # agents/code_reviewer.py now discards these at source, but
                    # historical pull requests still carry them.
                    if message.lower() in {"string", "message", "n/a"}:
                        continue
                    snapshot["findings"].append(
                        {
                            "pr": number,
                            "severity": match.group(1),
                            "source": match.group(2),
                            "file": match.group(3),
                            "line": int(match.group(4)) if match.group(4) else None,
                            "message": message,
                        }
                    )
    except ToolFailure as exc:
        print(f"warning: could not read GitHub ({exc})", file=sys.stderr)

    # A finding repeated across review rounds is one unfixed defect, not many.
    collapsed: dict[tuple, dict] = {}
    for finding in snapshot["findings"]:
        key = (finding["pr"], finding["file"], finding["message"])
        if key in collapsed:
            collapsed[key]["rounds"] += 1
        else:
            collapsed[key] = {**finding, "rounds": 1}
    snapshot["findings"] = list(collapsed.values())
    return snapshot


def main() -> int:
    snapshot = build_snapshot()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    print(
        f"wrote {OUT}: {len(snapshot['epics'])} epics, {len(snapshot['stories'])} stories, "
        f"{len(snapshot['pull_requests'])} PRs, {len(snapshot['findings'])} findings, "
        f"{len(snapshot['timeline'])} timeline events"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
