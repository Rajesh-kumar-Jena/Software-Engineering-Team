"""Shared fixtures: force every external service into dry-run and stub the LLM.

These tests exercise the wiring and the graph, not the model's writing ability,
so `invoke_agent_json` is replaced with a scripted responder. Everything below
it -- the agent nodes, the tool clients, the retry policy, the routing -- is the
real code path.
"""

from __future__ import annotations

import json
import os

import pytest

# Must be set before config.settings is imported anywhere. `load_dotenv` does
# not override an existing environment variable, so these win over whatever the
# developer's own .env happens to say -- the suite must assert on fixed
# behaviour, not on local configuration.
os.environ["TOOLS_DRY_RUN"] = "true"
os.environ["JIRA_PROJECT_KEY"] = "TEST"
os.environ["QA_FAIL_WITHOUT_SPECS"] = "true"
os.environ["DEV_RUN_TESTS"] = "false"
os.environ["QA_BASE_URL"] = "http://127.0.0.1:1"  # overridden per-test by qa_specs

import agents  # noqa: E402
import tools  # noqa: E402


BACKLOG = {
    "epics": [{"id": "E1", "title": "Subscriptions"}],
    "stories": [
        {
            "id": "S1",
            "epic_id": "E1",
            "title": "Create subscription",
            "description": "As a customer, I want to subscribe, so that I get access.",
            "acceptance_criteria": [
                "Given a valid payload, When POST /subscriptions, Then 201 is returned"
            ],
            "priority": 1,
            "business_dependencies": [],
        },
        {
            "id": "S2",
            "epic_id": "E1",
            "title": "Cancel subscription",
            "description": "As a customer, I want to cancel, so that I stop paying.",
            "acceptance_criteria": [
                "Given an active subscription, When DELETE /subscriptions/1, Then 204 is returned"
            ],
            "priority": 2,
            "business_dependencies": ["S1"],
        },
    ],
}


def refinement(requires_ui: bool = False) -> dict:
    """Architect output. Story ids are filled in at call time from the context."""
    return {
        "architecture_document": "# Architecture\nFastAPI service, Postgres store.",
        "development_plan": "# Plan\n1. create\n2. cancel",
        "requires_ui": requires_ui,
        "human_approval_requested": True,
    }


DEV_PASS = {
    "files": {
        "src/subscriptions.py": "def create():\n    return 201\n",
        "tests/test_subscriptions.py": "def test_create():\n    assert True\n",
    },
    "self_validation": {"status": "PASS", "attempts_used": 1},
    "story_status": "IN_REVIEW",
}
DEV_FAIL = {
    "files": {"src/subscriptions.py": "def create():\n    return 201\n"},
    "self_validation": {"status": "FAIL", "attempts_used": 1},
    "story_status": "IN_PROGRESS",
}
DEV_SYNTAX_ERROR = {
    "files": {"src/subscriptions.py": "def create(:\n    return\n"},
    "self_validation": {"status": "PASS", "attempts_used": 1},
    "story_status": "IN_REVIEW",
}
REVIEW_PASS = {"review_status": "PASS", "review_findings": []}
REVIEW_FAIL = {
    "review_status": "FAIL",
    "review_findings": [
        {
            "source": "CODE_REVIEWER",
            "severity": "BLOCKER",
            "file": "src/subscriptions.py",
            "line": 1,
            "message": "no error handling",
        }
    ],
}
QA_PASS = {"qa_status": "PASS", "failed_criteria": [], "jira_bug_id": None, "story_status": "DONE"}
QA_FAIL = {
    "qa_status": "FAIL",
    "failed_criteria": ["Given a valid payload, ... Then 201 is returned (unmet)"],
    "story_status": "BLOCKED",
}


class ScriptedLLM:
    """Replaces `agents.invoke_agent_json` with a per-agent response queue."""

    def __init__(self, script: dict[str, list[dict]]) -> None:
        self.script = script
        self.calls: dict[str, int] = {}
        self.contexts: dict[str, list[dict]] = {}
        self.schemas: dict[str, list[str | None]] = {}

    def __call__(
        self,
        agent_name: str,
        system_prompt: str,
        context: dict,
        attempts: int = 2,
        validate=None,
        schema: str | None = None,
    ):
        self.schemas.setdefault(agent_name, []).append(schema)
        self.contexts.setdefault(agent_name, []).append(context)
        index = self.calls.get(agent_name, 0)
        self.calls[agent_name] = index + 1

        sequence = self.script.get(agent_name)
        if not sequence:
            raise AssertionError(f"no scripted response for {agent_name}")
        response = sequence[min(index, len(sequence) - 1)]

        # The Architect must echo back every story id it was given.
        if agent_name == "solution_architect_agent":
            response = dict(response)
            response["stories"] = [
                {
                    "id": story["id"],
                    "technical_dependencies": [],
                    "requires_ui": response.get("requires_ui", False),
                    "requires_api": True,
                    "status": "REFINED",
                }
                for story in context["stories"]
            ]

        # Run the agent's own semantic check, exactly as the real invoker does,
        # so a scripted response that would be rejected in production is
        # rejected here too rather than quietly passing the tests.
        if validate is not None:
            validate(response)
        return response

    def count(self, agent_name: str) -> int:
        return self.calls.get(agent_name, 0)


@pytest.fixture(autouse=True)
def _isolated_clients():
    """Give every test a fresh set of dry-run clients with empty in-memory state."""
    tools.reset_clients()
    yield
    tools.reset_clients()


@pytest.fixture
def scripted(monkeypatch):
    """Install a `ScriptedLLM` across every agent module that calls the model."""

    def install(script: dict[str, list[dict]]) -> ScriptedLLM:
        llm = ScriptedLLM(script)
        for module in (
            "agents.product_manager",
            "agents.solution_architect",
            "agents.developer",
            "agents.code_reviewer",
            "agents.qa_agent",
        ):
            monkeypatch.setattr(f"{module}.invoke_agent_json", llm, raising=True)
        monkeypatch.setattr(agents, "invoke_agent_json", llm, raising=True)
        return llm

    return install


@pytest.fixture(scope="session")
def stub_service():
    """A real HTTP server for QA's API tests to exercise.

    `ApiTestRunner` is not stubbed out: it issues genuine requests, so a
    passing QA result in these tests means real HTTP actually succeeded.
    `/health` answers 200; `/broken` answers 500 so a failure path can be
    exercised the same way.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 -- BaseHTTPRequestHandler's naming
            code = 500 if self.path.startswith("/broken") else 200
            body = b'{"status": "ok"}' if code == 200 else b'{"error": "boom"}'
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence per-request logging
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def qa_specs(tmp_path, monkeypatch, stub_service):
    """Write API specs for the stories a test expects QA to verify."""

    def write(story_ids: list[str], passing: bool = True) -> str:
        for story_id in story_ids:
            (tmp_path / f"{story_id}.json").write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "name": f"{story_id} smoke",
                                "criterion": (
                                    "Given a valid payload, When POST, Then 201 is returned"
                                ),
                                "method": "GET",
                                "path": "/health" if passing else "/broken",
                                "expect_status": 200,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
        monkeypatch.setattr("tools.testing_tools.QA_SPEC_DIR", str(tmp_path))
        monkeypatch.setattr("tools.testing_tools.QA_BASE_URL", stub_service)
        return str(tmp_path)

    return write
