"""Rehearsal server: the whole dashboard, with nobody billed.

Serves the same API as `server.py` on the same port, so the dashboard cannot
tell the difference -- but the five agents are answered by a local stub instead
of a model, and Jira, GitHub and SonarQube are simulated. Nothing is sent over
the network and no quota is consumed, so you can practise the demo as many
times as you like.

    python demo_server.py        # then open the dashboard as usual

Stop `server.py` first -- both use port 8000. Swap back to `server.py` for the
real thing.

What it plays out: two stories, a Code Reviewer FAIL on the first one, the
Developer's fix, a PASS, a merge, then QA -- so the failure-and-recovery path
your mentor should see actually happens, every time, in seconds.
"""
from __future__ import annotations

import json
import logging
import os
import re

os.environ["TOOLS_DRY_RUN"] = "true"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")
for noisy in ("httpx", "urllib3", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

import agents
from core import prompts

CODE = '''"""Customer note service."""


def create_note(title: str, body: str) -> dict:
    if not title:
        raise ValueError("title is required")
    return {"title": title, "body": body}
'''

TEST = '''from src.notes.service import create_note


def test_create_note():
    assert create_note("a", "b")["title"] == "a"
'''


class _Reply:
    def __init__(self, content: str) -> None:
        self.content = content


class StubLLM:
    """Answers each agent with a schema-valid object, keyed by its system prompt."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.review_calls = 0

    def invoke(self, messages):  # noqa: ANN001 - mirrors the chat-model interface
        system = messages[0].content
        context = self._context(messages[1].content)

        if system == prompts.PRODUCT_MANAGER_SYSTEM_PROMPT:
            self.calls.append("pm")
            return _Reply(json.dumps(self._pm()))
        if system == prompts.SOLUTION_ARCHITECT_SYSTEM_PROMPT:
            self.calls.append("architect")
            return _Reply(json.dumps(self._architect(context)))
        if system == prompts.DEVELOPER_SYSTEM_PROMPT:
            self.calls.append("developer")
            return _Reply(json.dumps({
                "files": {"src/notes/service.py": CODE, "tests/test_notes_service.py": TEST},
                "self_validation": {"status": "PASS", "attempts_used": 1},
                "story_status": "IN_REVIEW",
            }))
        if system == prompts.CODE_REVIEWER_SYSTEM_PROMPT:
            self.calls.append("reviewer")
            self.review_calls += 1
            # Fail the very first review so the demo shows a real FAIL -> fix ->
            # PASS cycle rather than a straight line.
            if self.review_calls == 1:
                return _Reply(json.dumps({
                    "review_status": "FAIL",
                    "review_findings": [{
                        "severity": "MAJOR",
                        "file": "src/notes/service.py",
                        "line": 5,
                        "message": "create_note never validates the body argument.",
                    }],
                    "pr": {"id": "GH-1", "status": "OPEN"},
                }))
            return _Reply(json.dumps({
                "review_status": "PASS", "review_findings": [],
                "pr": {"id": "GH-1", "status": "OPEN"},
            }))
        if system == prompts.QA_SYSTEM_PROMPT:
            self.calls.append("qa")
            return _Reply(json.dumps({
                "qa_status": "PASS", "qa_test_type": "API", "qa_results_ref": "stub-run",
                "failed_criteria": [], "jira_bug_id": None, "story_status": "DONE",
            }))
        raise AssertionError("unrecognised system prompt -- stub is out of date")

    @staticmethod
    def _context(user_content: str) -> dict:
        match = re.search(r"AVAILABLE CONTEXT \(JSON\):\n(.*?)\n\nReturn ONLY", user_content, re.S)
        if not match:
            return {}
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _pm() -> dict:
        return {
            "epics": [
                {"id": "E1", "title": "Note Management"},
                {"id": "E2", "title": "Note Retrieval"},
            ],
            "stories": [
                {"id": "S1", "epic_id": "E1", "title": "Create a note",
                 "description": "As a user, I want to create a note, so that I can save it.",
                 "acceptance_criteria": [
                     "Given a title and body, When I create a note, Then it is stored",
                     "Given no title, When I create a note, Then it is rejected"],
                 "priority": 1, "business_dependencies": []},
                {"id": "S2", "epic_id": "E2", "title": "Retrieve a note",
                 "description": "As a user, I want to read a note, so that I can review it.",
                 "acceptance_criteria": [
                     "Given a stored note, When I fetch it, Then it is returned",
                     "Given an unknown id, When I fetch it, Then a 404 is returned"],
                 "priority": 2, "business_dependencies": ["S1"]},
            ],
        }

    @staticmethod
    def _architect(context: dict) -> dict:
        ids = [s["id"] for s in context.get("stories", []) if isinstance(s, dict) and s.get("id")]
        return {
            "architecture_document": "# System Architecture\n\nSingle FastAPI service.",
            "development_plan": "# Development Plan\n\nCreation first, then retrieval.",
            "stories": [
                {"id": sid, "technical_dependencies": [], "requires_ui": False,
                 "requires_api": True, "status": "REFINED"} for sid in ids
            ],
            "human_approval_requested": True,
        }


STUB = StubLLM()
agents.get_llm = lambda *a, **k: STUB  # the only seam

import server  # noqa: E402  - imported after the seam is in place
import uvicorn  # noqa: E402

print("STUB SERVER READY -- no model will be called")
uvicorn.run(server.app, host="127.0.0.1", port=8000, log_level="warning")
