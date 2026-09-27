"""End-to-end workflow verification.

The headline requirement for Phase 5: the graph runs from START to either the
human gate pause or backlog completion without an unhandled exception, with the
tool clients genuinely invoked along the way.
"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from conftest import (
    BACKLOG,
    DEV_FAIL,
    DEV_PASS,
    DEV_SYNTAX_ERROR,
    QA_FAIL,
    QA_PASS,
    REVIEW_FAIL,
    REVIEW_PASS,
    refinement,
)
from core.graph import DEFAULT_RECURSION_LIMIT, build_graph
from core.state import RepoRef, RetryLimits, WorkflowStatus, create_initial_state

REPO = RepoRef(owner="acme", name="subs-api", default_branch="main")
LIMITS = RetryLimits(
    MAX_DEV_ATTEMPTS=3, MAX_REVIEW_ITERATIONS=3, MAX_QA_ITERATIONS=2, MAX_TOOL_RETRIES=3
)
REQUIREMENT = "Build a backend REST API for managing customer subscriptions. No UI."


def run(script, resume=None, thread="t"):
    """Invoke the graph, optionally resuming past the approval gate."""
    graph = build_graph(checkpointer=InMemorySaver())
    config = {
        "configurable": {"thread_id": thread},
        "recursion_limit": DEFAULT_RECURSION_LIMIT,
    }
    state = create_initial_state(REQUIREMENT, REPO, LIMITS)
    result = graph.invoke(state, config)
    if result.get("__interrupt__") and resume is not None:
        result = graph.invoke(Command(resume=resume), config)
    return result


APPROVE = {"status": "APPROVED", "approved_by": "tester"}

HAPPY = {
    "pm_agent": [BACKLOG],
    "solution_architect_agent": [refinement()],
    "developer_agent": [DEV_PASS],
    "code_reviewer_agent": [REVIEW_PASS],
    "qa_agent": [QA_PASS],
}


# ---------------------------------------------------------------------------
# The two required terminal states
# ---------------------------------------------------------------------------


def test_reaches_human_gate_pause(scripted):
    """START -> PM -> Architect -> suspended at the hard human gate."""
    llm = scripted(HAPPY)
    result = run(HAPPY)

    assert result.get("__interrupt__"), "the graph must suspend at human_approval_gate"
    assert result["workflow_status"] == WorkflowStatus.AWAITING_APPROVAL
    assert result["human_approval"]["status"] == "PENDING"
    # Jira really was called: keys come from the client, not from the model.
    assert [e["id"] for e in result["epics"]] == ["TEST-1"]
    assert [s["id"] for s in result["stories"]] == ["TEST-2", "TEST-3"]
    assert result["architecture_doc_ref"] and result["dev_plan_ref"]
    assert all(s["status"] == "REFINED" for s in result["stories"])
    assert llm.count("pm_agent") == 1 and llm.count("solution_architect_agent") == 1


def test_reaches_backlog_complete(scripted, qa_specs):
    """START -> ... -> every story DONE -> BACKLOG_COMPLETE, no exceptions."""
    qa_specs(["TEST-2", "TEST-3"])
    llm = scripted(HAPPY)
    result = run(HAPPY, resume=APPROVE)

    assert result["workflow_status"] == WorkflowStatus.BACKLOG_COMPLETE
    assert result["halt_reason"] is None
    assert result["error_log"] == []
    assert [s["status"] for s in result["stories"]] == ["DONE", "DONE"]
    assert result["pr"]["status"] == "MERGED"
    assert llm.count("developer_agent") == 2
    assert llm.count("code_reviewer_agent") == 2
    assert llm.count("qa_agent") == 2


# ---------------------------------------------------------------------------
# Tool binding -- each agent reaching its own tools of record
# ---------------------------------------------------------------------------


def test_jira_is_the_source_of_story_keys(scripted):
    """The model's ids are internal handles; Jira mints what state carries."""
    scripted(HAPPY)
    result = run(HAPPY)

    from tools import get_jira

    store = get_jira()._dry
    assert set(store.issues) == {"TEST-1", "TEST-2", "TEST-3"}
    # The business dependency was rewritten from the model's "S1" to a Jira key.
    cancel = next(s for s in result["stories"] if s["id"] == "TEST-3")
    assert cancel["business_dependencies"] == ["TEST-2"]
    assert ("TEST-3", "TEST-2", "Business dependency (PM Agent)") in store.links


def test_qa_reads_the_pms_original_criteria(scripted, qa_specs):
    """QA's criteria come back from Jira, not from state or the developer."""
    qa_specs(["TEST-2", "TEST-3"])
    llm = scripted(HAPPY)
    run(HAPPY, resume=APPROVE)

    context = llm.contexts["qa_agent"][0]
    assert context["acceptance_criteria"] == BACKLOG["stories"][0]["acceptance_criteria"]


def test_github_receives_branch_commit_and_pr(scripted, qa_specs):
    """The Developer's code actually lands on a branch and a PR."""
    qa_specs(["TEST-2", "TEST-3"])
    scripted(HAPPY)
    run(HAPPY, resume=APPROVE)

    from tools import get_github

    store = get_github()._dry
    assert "feature/TEST-2" in store.branches
    commits = store.branches["feature/TEST-2"]
    assert commits and "src/subscriptions.py" in commits[0]["files"]
    assert all(pull["state"] == "merged" for pull in store.pulls.values())


def test_sonarqube_gate_is_consulted_by_the_reviewer(scripted, qa_specs):
    """The reviewer sees a real gate verdict in its context."""
    qa_specs(["TEST-2", "TEST-3"])
    llm = scripted(HAPPY)
    run(HAPPY, resume=APPROVE)

    sonar = llm.contexts["code_reviewer_agent"][0]["sonarqube"]
    assert sonar["quality_gate_passed"] is True
    assert sonar["simulated"] is True, "no token configured in tests"


def test_reviewer_reads_the_real_diff(scripted, qa_specs):
    """The diff handed to the model comes from GitHub, not the model itself."""
    qa_specs(["TEST-2", "TEST-3"])
    llm = scripted(HAPPY)
    run(HAPPY, resume=APPROVE)

    diff = llm.contexts["code_reviewer_agent"][0]["pull_request_diff"]
    assert "src/subscriptions.py" in diff


# ---------------------------------------------------------------------------
# Adaptive scope
# ---------------------------------------------------------------------------


def test_backend_only_story_never_invokes_playwright(scripted, qa_specs, monkeypatch):
    """requires_ui == false must bypass Playwright entirely."""
    qa_specs(["TEST-2", "TEST-3"])
    scripted(HAPPY)

    called = []
    monkeypatch.setattr(
        "tools.testing_tools.PlaywrightRunner.run_ui_tests",
        lambda self, spec: called.append(spec),
    )
    result = run(HAPPY, resume=APPROVE)

    assert result["workflow_status"] == WorkflowStatus.BACKLOG_COMPLETE
    assert called == [], "Playwright must not run for a backend-only story"
    assert {s["qa_test_type"] for s in [result]} == {"API"}


def test_ui_story_invokes_playwright(scripted, qa_specs, monkeypatch):
    """requires_ui == true must run Playwright."""
    qa_specs(["TEST-2", "TEST-3"])
    script = {**HAPPY, "solution_architect_agent": [refinement(requires_ui=True)]}
    scripted(script)

    called = []
    monkeypatch.setattr(
        "tools.testing_tools.PlaywrightRunner.run_ui_tests",
        lambda self, spec: called.append(spec) or {"passed": True, "skipped": False, "failures": [], "results": []},
    )
    result = run(script, resume=APPROVE)

    assert called == ["TEST-2", "TEST-3"]
    assert result["qa_test_type"] == "UI"


# ---------------------------------------------------------------------------
# Retry limits and safe termination
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script,counter,limit,expected_story_status",
    [
        ({**HAPPY, "developer_agent": [DEV_FAIL]}, "dev_attempts", 3, "IN_PROGRESS"),
        ({**HAPPY, "code_reviewer_agent": [REVIEW_FAIL]}, "review_iterations", 3, "IN_REVIEW"),
        ({**HAPPY, "qa_agent": [QA_FAIL]}, "qa_iterations", 2, "BLOCKED"),
    ],
)
def test_hard_limits_halt_safely(
    scripted, qa_specs, script, counter, limit, expected_story_status
):
    """Each limit halts with a HARD_LIMIT entry and preserves state."""
    qa_specs(["TEST-2", "TEST-3"])
    scripted(script)
    result = run(script, resume=APPROVE)

    assert result["workflow_status"] == WorkflowStatus.HALTED
    assert result[counter] == limit
    assert counter in result["halt_reason"]
    assert result["error_log"][-1]["kind"] == "HARD_LIMIT"
    first = next(s for s in result["stories"] if s["id"] == "TEST-2")
    assert first["status"] == expected_story_status
    # Nothing is rolled back: the pointers a human needs are still here.
    assert result["current_story_id"] == "TEST-2"


def test_tool_failure_halts_as_tool_failure(scripted, monkeypatch):
    """An exhausted tool budget is a TOOL_FAILURE halt, not a review verdict."""
    from tools import ToolFailure

    scripted(HAPPY)

    def explode(self, title):
        raise ToolFailure("jira.post", "connection refused", 3)

    monkeypatch.setattr("tools.jira_client.JiraClient.create_epic", explode)
    result = run(HAPPY)

    assert result["workflow_status"] == WorkflowStatus.HALTED
    assert result["error_log"][-1]["kind"] == "TOOL_FAILURE"
    assert "retry budget" in result["halt_reason"]


def test_invalid_model_output_halts_as_validation_failure(scripted, monkeypatch):
    """A model that cannot produce valid output halts; it never raises through.

    Regression test: an `AgentOutputError` used to escape the graph as an
    unhandled traceback instead of terminating the run safely.
    """
    from agents import AgentOutputError

    scripted(HAPPY)

    def explode(*args, **kwargs):
        raise AgentOutputError("no valid output in 3 attempt(s): missing 'stories'")

    monkeypatch.setattr("agents.solution_architect.invoke_agent_json", explode)
    result = run(HAPPY)

    assert result["workflow_status"] == WorkflowStatus.HALTED
    assert result["error_log"][-1]["kind"] == "VALIDATION_FAILURE"
    # The PM's work is preserved -- nothing is rolled back on a halt.
    assert len(result["stories"]) == 2


def test_boundary_breach_halts_rather_than_raising(scripted, monkeypatch):
    """A PermissionError from the approval guard halts instead of crashing."""
    scripted(HAPPY)

    def breach(state):
        raise PermissionError("acted before approval")

    monkeypatch.setattr("core.graph.select_ready_story_node", breach)
    result = run(HAPPY, resume=APPROVE)

    assert result["workflow_status"] == WorkflowStatus.HALTED
    assert "breached a system boundary" in result["halt_reason"]


def test_halt_in_planning_reaches_end(scripted, monkeypatch):
    """A halt before the approval gate must terminate, not fall through.

    Regression test: `pm_agent` and `solution_architect_agent` were joined by
    static edges, so a halt in the PM node still ran the Architect, which then
    raised a misleading "empty backlog" error on top of the real fault.
    """
    from tools import ToolFailure

    scripted(HAPPY)
    monkeypatch.setattr(
        "tools.jira_client.JiraClient.create_epic",
        lambda self, title: (_ for _ in ()).throw(ToolFailure("jira", "down", 3)),
    )
    result = run(HAPPY)

    assert result["workflow_status"] == WorkflowStatus.HALTED
    assert result["error_log"][-1]["kind"] == "TOOL_FAILURE"
    # The Architect never ran: only one error, and no design docs exist.
    assert len(result["error_log"]) == 1
    assert result["architecture_doc_ref"] is None


@pytest.mark.parametrize(
    "spec,expected",
    [
        # An Ollama tag carries its own colon; it must not be read as a provider.
        ("qwen2.5-coder:3b", ("ollama", "qwen2.5-coder:3b")),
        ("llama3.1:8b", ("ollama", "llama3.1:8b")),
        ("ollama:llama3.1:8b", ("ollama", "llama3.1:8b")),
        ("gemini:gemini-2.5-flash", ("gemini", "gemini-2.5-flash")),
        ("google:gemini-2.5-flash", ("google", "gemini-2.5-flash")),
        ("groq:llama-3.3-70b-versatile", ("groq", "llama-3.3-70b-versatile")),
    ],
)
def test_model_spec_parsing(spec, expected):
    from config.settings import _split_model_spec

    assert _split_model_spec(spec) == expected


def test_pm_nudges_once_on_a_single_story_backlog(monkeypatch):
    """A one-story backlog gets one corrective retry, then is accepted.

    Under-decomposition was the defect: the schema example showed a single
    story, so the model reproduced a single story. A hard minimum would be wrong
    (some requirements really are atomic), so the guard nudges exactly once.
    """
    from agents.product_manager import pm_agent_node
    from core.state import RepoRef, create_initial_state

    one = {
        "epics": [{"id": "E1", "title": "Coupons"}],
        "stories": [
            {
                "id": "S1",
                "epic_id": "E1",
                "title": "Build the whole service",
                "description": "As a user...",
                "acceptance_criteria": ["Given x, When y, Then z"],
                "priority": 1,
                "business_dependencies": [],
            }
        ],
    }
    seen = {"n": 0, "errors": []}

    def fake(agent_name, prompt, context, attempts=2, validate=None, schema=None):
        for _ in range(attempts):
            seen["n"] += 1
            try:
                if validate:
                    validate(one)
                return one
            except AgentOutputError as exc:
                seen["errors"].append(str(exc))
        return one

    from agents import AgentOutputError

    monkeypatch.setattr("agents.product_manager.invoke_agent_json", fake)
    update = pm_agent_node(
        create_initial_state("Build a coupon service", RepoRef(owner="o", name="r", default_branch="main"))
    )

    assert seen["n"] == 2, "exactly one nudge, then acceptance"
    assert "single story" in seen["errors"][0]
    assert len(update["stories"]) == 1, "an atomic requirement may still yield one story"


def test_pm_rejects_placeholder_titles(monkeypatch):
    """A title still carrying the schema's <brackets> is a template, not a story."""
    from agents import AgentOutputError
    from agents.product_manager import pm_agent_node
    from core.state import RepoRef, create_initial_state

    echoed = {
        "epics": [{"id": "E1", "title": "X"}],
        "stories": [
            {
                "id": "S1",
                "epic_id": "E1",
                "title": "<the first capability, as a short verb phrase>",
                "description": "As a <role>...",
                "acceptance_criteria": ["Given <x>, When <y>, Then <z>"],
                "priority": 1,
                "business_dependencies": [],
            }
        ],
    }
    captured = []

    def fake(agent_name, prompt, context, attempts=2, validate=None, schema=None):
        try:
            if validate:
                validate(echoed)
        except AgentOutputError as exc:
            captured.append(str(exc))
            raise
        return echoed

    monkeypatch.setattr("agents.product_manager.invoke_agent_json", fake)
    with pytest.raises(AgentOutputError):
        pm_agent_node(
            create_initial_state("Build a coupon service", RepoRef(owner="o", name="r", default_branch="main"))
        )
    assert "placeholder brackets" in captured[0]


def test_gemini_content_blocks_are_flattened_to_json():
    """Gemini returns content blocks, not a string.

    Regression test: `str()` on the block list yields a Python repr with single
    quotes, which is not JSON. Every hosted call would have failed to parse.
    """
    import json

    from agents import _extract_json, response_text

    blocks = [{"type": "text", "text": '{"review_status": "PASS"}'}]
    assert json.loads(_extract_json(response_text(blocks))) == {"review_status": "PASS"}

    # Split across blocks, as a longer reply arrives.
    split = [{"type": "text", "text": '{"a": 1,'}, {"type": "text", "text": ' "b": 2}'}]
    assert json.loads(_extract_json(response_text(split))) == {"a": 1, "b": 2}

    # Plain strings (Ollama) must still pass through untouched.
    assert response_text('{"a": 1}') == '{"a": 1}'


def test_hosted_provider_without_a_key_fails_clearly(monkeypatch):
    """A missing key must name the variable and where to get one."""
    from config import settings

    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
    settings.get_llm.cache_clear()
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        settings.get_llm(model="gemini:gemini-2.5-flash")
    settings.get_llm.cache_clear()


def test_gemini_3_thinking_level_uses_model_kwargs(monkeypatch):
    """Gemini 3 options must not be passed as unsupported constructor kwargs."""
    import langchain_google_genai

    from config import settings

    captured = {}

    class FakeGemini:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(langchain_google_genai, "ChatGoogleGenerativeAI", FakeGemini)
    settings.get_llm.cache_clear()
    try:
        settings.get_llm(model="gemini:gemini-3.6-flash")
    finally:
        settings.get_llm.cache_clear()

    assert captured["model_kwargs"] == {"thinking_level": settings.GEMINI_THINKING_LEVEL}
    assert "reasoning_effort" not in captured


def test_agents_route_to_their_configured_model(monkeypatch):
    """AGENT_MODELS decides which model an agent's call is built with."""
    import agents
    from config import settings

    captured = {}

    class Fake:
        def invoke(self, messages):
            class R:
                content = '{"ok": true}'

            return R()

    def fake_get_llm(model=None, **kw):
        captured["model"] = model
        return Fake()

    monkeypatch.setattr(
        settings, "AGENT_MODELS", {"code_reviewer_agent": "gemini:gemini-2.5-flash"}
    )
    monkeypatch.setattr(agents, "AGENT_MODELS", settings.AGENT_MODELS)
    monkeypatch.setattr(agents, "get_llm", fake_get_llm)

    agents.invoke_agent_json("code_reviewer_agent", "sys", {})
    assert captured["model"] == "gemini:gemini-2.5-flash"

    agents.invoke_agent_json("pm_agent", "sys", {})
    assert captured["model"] is None, "an unlisted agent keeps the default model"


def test_model_cannot_forge_a_sonarqube_finding():
    """A model finding claiming SONARQUBE is re-tagged, never trusted.

    Regression test from the live Phase 6 run: a 3B reviewer emitted
    `{"source": "SONARQUBE", ...}` copied from the schema example while the
    scanner had returned nothing. A fabricated static-analysis result must never
    be able to gate a merge.
    """
    from agents.code_reviewer import _normalize_findings

    findings = _normalize_findings(
        [
            {
                "source": "SONARQUBE",
                "severity": "MAJOR",
                "file": "src/api/user.py",
                "line": 42,
                "message": "Variable 'user_id' is unused",
            }
        ]
    )
    assert len(findings) == 1
    assert findings[0]["source"] == "CODE_REVIEWER"


def test_placeholder_findings_are_discarded():
    """A finding echoed out of the schema example is noise, not a defect."""
    from agents.code_reviewer import _normalize_findings

    findings = _normalize_findings(
        [
            {"severity": "BLOCKER", "file": "src/api/user.py", "line": 10, "message": "string"},
            {"severity": "MAJOR", "file": "string", "line": 42, "message": "real problem"},
            {
                "severity": "BLOCKER",
                "file": "src/api/user.py",
                "line": 10,
                "message": "get_user() dereferences a None profile",
            },
        ]
    )
    assert [f["message"] for f in findings] == ["get_user() dereferences a None profile"]


@pytest.mark.parametrize(
    "name,message",
    [
        # Both of these escaped in live runs and halted the workflow as a
        # VALIDATION_FAILURE with a traceback, which is the wrong class twice
        # over: the request was fine, and the condition was retryable.
        ("GoogleRateLimitError", "429 RESOURCE_EXHAUSTED. quota exceeded"),
        ("GoogleAPIError", "503 UNAVAILABLE. This model is currently experiencing high demand."),
        ("ServiceUnavailable", "502 Bad Gateway"),
        ("APIError", "500 internal error"),
        ("DeadlineExceeded", "deadline exceeded"),
    ],
)
def test_provider_outages_are_classified_transient(name, message):
    from agents import _is_transient

    exc = type(name, (Exception,), {})(message)
    assert _is_transient(exc), f"{name} should be retryable, not a validation failure"


def test_a_genuinely_bad_request_is_not_treated_as_transient():
    """Classification must not swallow real errors into an endless retry."""
    from agents import _is_transient

    assert not _is_transient(ValueError("invalid argument: bad schema"))
    assert not _is_transient(KeyError("stories"))


def test_transient_errors_are_retried_then_reported_clearly(monkeypatch):
    """A throttled call backs off and retries, and finally names the cause."""
    from agents import AgentOutputError, invoke_agent_json

    calls = {"n": 0}
    slept: list[float] = []

    class Throttled:
        def invoke(self, messages):
            calls["n"] += 1
            raise type("GoogleRateLimitError", (Exception,), {})(
                "429 RESOURCE_EXHAUSTED. Please retry in 7s"
            )

    monkeypatch.setattr("agents.get_llm", lambda *a, **kw: Throttled())
    monkeypatch.setattr("agents.time.sleep", lambda s: slept.append(s))

    with pytest.raises(AgentOutputError) as excinfo:
        invoke_agent_json("pm_agent", "sys", {}, attempts=3)

    assert calls["n"] == 3, "every attempt should be spent before giving up"
    assert slept == [8.0, 8.0], "backoff should honour the provider's retry hint"
    assert "unavailable" in str(excinfo.value).lower()


def test_developer_sees_the_existing_repository(scripted, qa_specs):
    """The Developer must be shown what it has to stay compatible with.

    Regression test from a live multi-story run: the Developer authors whole
    files but was given no view of the repository, so story 2 rewrote a module
    story 1 had created, broke the import in `__init__.py`, and switched
    framework. Each "fix" broke a different contract and the review loop
    diverged (1 -> 2 -> 3 blocking findings) instead of converging.
    """
    qa_specs(["TEST-2", "TEST-3"])
    llm = scripted(HAPPY)
    run(HAPPY, resume=APPROVE)

    # The second story's context must include what the first story committed.
    second = llm.contexts["developer_agent"][1]
    assert "repository_files" in second and "existing_file_contents" in second
    assert "src/subscriptions.py" in second["repository_files"], (
        "story 2 should see the file story 1 merged"
    )
    assert second["existing_file_contents"].get("src/subscriptions.py"), (
        "it should see that file's contents, not just its name"
    )


def test_repository_context_is_bounded(monkeypatch):
    """A large repository must not blow the Developer's prompt."""
    from agents.developer import _repository_context

    class FatRepo:
        def list_repository_files(self, ref):
            return [f"src/mod{i:03d}.py" for i in range(200)]

        def read_repository_file(self, path, ref):
            return "x" * 5000

    monkeypatch.setattr("agents.developer.DEV_CONTEXT_MAX_FILES", 5)
    monkeypatch.setattr("agents.developer.DEV_CONTEXT_MAX_CHARS", 6000)
    paths, contents = _repository_context(FatRepo(), "main", {})

    assert len(paths) == 200, "the full tree is cheap and stays complete"
    assert len(contents) <= 5
    assert sum(len(v) for v in contents.values()) <= 6000


def test_unreadable_repository_degrades_instead_of_halting(monkeypatch):
    """Losing the repo view weakens the prompt; it must not fail the story."""
    from agents.developer import _repository_context

    class Broken:
        def list_repository_files(self, ref):
            raise RuntimeError("github is down")

    paths, contents = _repository_context(Broken(), "main", {})
    assert paths == [] and contents == {}


def test_provider_rejected_generation_is_reprompted(monkeypatch):
    """A server-side JSON rejection is bad output, so it reprompts.

    Regression test: Groq validates `response_format: json_object` itself and
    answers 400 `json_validate_failed`, so no content ever reaches the parser.
    That escaped as an unhandled error and halted a live run even though the
    right response was simply to ask again with the reason.
    """
    from agents import invoke_agent_json

    calls = {"n": 0}

    class Picky:
        def invoke(self, messages):
            calls["n"] += 1
            if calls["n"] == 1:
                raise type("BadRequestError", (Exception,), {})(
                    "Error code: 400 - {'error': {'code': 'json_validate_failed', "
                    "'failed_generation': ''}}"
                )

            class R:
                content = '{"qa_status": "PASS"}'

            return R()

    monkeypatch.setattr("agents.get_llm", lambda *a, **kw: Picky())
    result = invoke_agent_json("qa_agent", "sys", {}, attempts=2)

    assert result == {"qa_status": "PASS"}
    assert calls["n"] == 2, "the rejection must cost one attempt, not the run"


def test_bad_output_is_not_confused_with_an_outage():
    """Classification must keep the two failure families apart."""
    from agents import _is_bad_output, _is_transient

    rejected = type("BadRequestError", (Exception,), {})("400 json_validate_failed")
    outage = type("APIError", (Exception,), {})("503 UNAVAILABLE high demand")

    assert _is_bad_output(rejected) and not _is_bad_output(outage)
    assert _is_transient(outage)


def test_malformed_json_is_reprompted_not_raised(monkeypatch):
    """Unparseable output must be fed back to the model, not escape the loop.

    Regression test. An `except Exception` clause was once ordered ahead of the
    `except (JSONDecodeError, AgentOutputError)` clause, which made the specific
    handler unreachable and silently killed every reprompt path -- malformed
    JSON, the PM's granularity nudge, the Architect's id check. Every existing
    reprompt test stubbed `invoke_agent_json` out entirely, so none of them
    touched the real loop and none of them caught it. This one drives the real
    loop end to end.
    """
    from agents import invoke_agent_json

    replies = ["not json at all", '{"review_status": "PASS"}']
    seen: list[int] = []

    class Flaky:
        def invoke(self, messages):
            seen.append(len(messages))

            class R:
                content = replies[len(seen) - 1]

            return R()

    monkeypatch.setattr("agents.get_llm", lambda *a, **kw: Flaky())
    result = invoke_agent_json("code_reviewer_agent", "sys", {}, attempts=2)

    assert result == {"review_status": "PASS"}, "the retry's good answer should be returned"
    assert len(seen) == 2, "the bad reply must trigger exactly one reprompt"
    assert seen[1] > seen[0], "the correction must be appended to the conversation"


def test_validate_rejection_is_reprompted_through_the_real_loop(monkeypatch):
    """A semantic rejection also reprompts rather than escaping."""
    from agents import invoke_agent_json

    replies = ['{"stories": []}', '{"stories": [{"id": "A"}]}']
    calls = {"n": 0}

    class Flaky:
        def invoke(self, messages):
            calls["n"] += 1

            class R:
                content = replies[calls["n"] - 1]

            return R()

    def validate(candidate):
        from agents import AgentOutputError

        if not candidate.get("stories"):
            raise AgentOutputError("stories must not be empty")

    monkeypatch.setattr("agents.get_llm", lambda *a, **kw: Flaky())
    result = invoke_agent_json("pm_agent", "sys", {}, attempts=2, validate=validate)

    assert result["stories"] == [{"id": "A"}]
    assert calls["n"] == 2


def test_llm_timeout_halts_instead_of_crashing(monkeypatch):
    """A stalled model call is retried, then halts -- it never escapes the graph.

    Regression test for the live Phase 6 run: one unbounded generation ran for
    22 minutes with no timeout, and had it raised, the exception would have
    aborted the run rather than halting it safely.
    """
    import httpx

    from agents import AgentOutputError, invoke_agent_json

    calls = {"n": 0}

    class Stalled:
        def invoke(self, messages):
            calls["n"] += 1
            raise httpx.ReadTimeout("model call exceeded LLM_TIMEOUT")

    monkeypatch.setattr("agents.get_llm", lambda *a, **kw: Stalled())
    monkeypatch.setattr("agents.time.sleep", lambda s: None)

    with pytest.raises(AgentOutputError) as excinfo:
        invoke_agent_json("pm_agent", "sys", {"requirement": "x"}, attempts=2)

    assert calls["n"] == 2, "a transport failure must be retried, not given up on"
    # A timeout is transient, so it is reported as a provider availability
    # problem rather than as bad model output.
    assert "unavailable" in str(excinfo.value).lower()


def test_llm_timeout_inside_the_graph_is_a_validation_halt(scripted, monkeypatch):
    """End to end: a model timeout ends the run with state preserved."""
    import httpx

    def stall(*args, **kwargs):
        raise httpx.ReadTimeout("stalled")

    scripted(HAPPY)
    monkeypatch.setattr("agents.product_manager.invoke_agent_json", stall)
    result = run(HAPPY)

    assert result["workflow_status"] == WorkflowStatus.HALTED
    assert result["error_log"][-1]["kind"] == "VALIDATION_FAILURE"


def test_transient_failure_is_retried_within_budget(scripted, monkeypatch):
    """A transient error is repeated up to MAX_TOOL_RETRIES, then succeeds."""
    import requests

    from tools import with_retries

    monkeypatch.setattr("tools.time.sleep", lambda _: None)
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise requests.ConnectionError("boom")
        return "ok"

    assert with_retries("test", flaky) == "ok"
    assert attempts["n"] == 3


def test_non_transient_failure_is_not_retried(monkeypatch):
    """A 404 is a deterministic answer; repeating it only burns the budget."""
    import requests

    from tools import ToolFailure, with_retries

    monkeypatch.setattr("tools.time.sleep", lambda _: None)
    attempts = {"n": 0}

    def not_found():
        attempts["n"] += 1
        response = requests.Response()
        response.status_code = 404
        raise requests.HTTPError("nope", response=response)

    with pytest.raises(ToolFailure):
        with_retries("test", not_found)
    assert attempts["n"] == 1


# ---------------------------------------------------------------------------
# The two Developer re-entry shapes
# ---------------------------------------------------------------------------


def test_review_fix_updates_the_same_pr(scripted, qa_specs):
    """A code-review FAIL must never open a second PR."""
    qa_specs(["TEST-2", "TEST-3"])
    script = {**HAPPY, "code_reviewer_agent": [REVIEW_FAIL, REVIEW_PASS]}
    scripted(script)
    result = run(script, resume=APPROVE)

    from tools import get_github

    store = get_github()._dry
    prs_for_first_story = [p for p in store.pulls.values() if p["branch"] == "feature/TEST-2"]
    assert len(prs_for_first_story) == 1, "a review fix must reuse the same PR"
    assert prs_for_first_story[0].get("updates"), "the fix should have been recorded on it"
    assert result["workflow_status"] == WorkflowStatus.BACKLOG_COMPLETE


def test_qa_fix_opens_a_new_pr_and_is_fully_re_reviewed(scripted, qa_specs):
    """A QA FAIL must open a NEW FIX PR that goes through review again."""
    qa_specs(["TEST-2", "TEST-3"])
    script = {**HAPPY, "qa_agent": [QA_FAIL, QA_PASS]}
    llm = scripted(script)
    result = run(script, resume=APPROVE)

    from tools import get_github

    store = get_github()._dry
    branches = set(store.branches)
    assert "feature/TEST-2" in branches and "fix/TEST-2-2" in branches
    # story 1 reviewed twice (feature + fix), story 2 once
    assert llm.count("code_reviewer_agent") == 3
    assert result["workflow_status"] == WorkflowStatus.BACKLOG_COMPLETE


def test_qa_failure_always_files_a_linked_bug(scripted, qa_specs):
    """Traceability: every FAIL leaves a linked defect, even when halting."""
    qa_specs(["TEST-2", "TEST-3"])
    script = {**HAPPY, "qa_agent": [QA_FAIL]}
    scripted(script)
    result = run(script, resume=APPROVE)

    from tools import get_jira

    store = get_jira()._dry
    bugs = [k for k, v in store.issues.items() if v["issuetype"] == "Bug"]
    assert bugs, "a QA failure must file a Jira bug"
    assert result["jira_bug_id"] in bugs
    assert any(link[0] == result["jira_bug_id"] for link in store.links)


# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------


def test_developer_blocked_before_approval(scripted):
    """The approval gate is absolute -- no repo write is reachable before it."""
    from agents.developer import developer_node

    scripted(HAPPY)
    state = create_initial_state(REQUIREMENT, REPO, LIMITS)
    state["current_story_id"] = "TEST-2"
    state["stories"] = [
        {
            "id": "TEST-2",
            "epic_id": "TEST-1",
            "title": "x",
            "priority": 1,
            "business_dependencies": [],
            "technical_dependencies": [],
            "requires_ui": False,
            "requires_api": True,
            "status": "READY",
        }
    ]
    with pytest.raises(PermissionError):
        developer_node(state)


def test_syntax_error_overrides_a_model_claiming_pass(scripted, qa_specs):
    """Self-validation is objective where it can be: bad code cannot pass."""
    qa_specs(["TEST-2", "TEST-3"])
    script = {**HAPPY, "developer_agent": [DEV_SYNTAX_ERROR]}
    scripted(script)
    result = run(script, resume=APPROVE)

    assert result["workflow_status"] == WorkflowStatus.HALTED
    assert result["dev_attempts"] == 3
    assert "dev_attempts" in result["halt_reason"]


def test_unsafe_file_path_is_refused(scripted):
    """A path is where a write lands, so traversal is refused, not rewritten."""
    from agents import AgentOutputError
    from agents.developer import _safe_files

    with pytest.raises(AgentOutputError):
        _safe_files({"../../etc/passwd": "x"})
    with pytest.raises(AgentOutputError):
        _safe_files({"/etc/passwd": "x"})
    assert _safe_files({"src/a.py": "x"}) == {"src/a.py": "x"}


def test_qa_refuses_to_pass_without_executable_specs(scripted, monkeypatch, tmp_path):
    """No spec means no evidence; QA fails rather than rubber-stamping."""
    monkeypatch.setattr("tools.testing_tools.QA_SPEC_DIR", str(tmp_path))
    monkeypatch.setattr("agents.qa_agent.QA_FAIL_WITHOUT_SPECS", True)
    scripted(HAPPY)
    result = run(HAPPY, resume=APPROVE)

    assert result["workflow_status"] == WorkflowStatus.HALTED
    assert result["qa_iterations"] == 2
    assert result["jira_bug_id"]


def test_qa_without_specs_can_fall_back_to_model_judgment(scripted, monkeypatch, tmp_path):
    """The permissive setting lets a demo run complete, and says so in the log."""
    monkeypatch.setattr("tools.testing_tools.QA_SPEC_DIR", str(tmp_path))
    monkeypatch.setattr("agents.qa_agent.QA_FAIL_WITHOUT_SPECS", False)
    scripted(HAPPY)
    result = run(HAPPY, resume=APPROVE)

    assert result["workflow_status"] == WorkflowStatus.BACKLOG_COMPLETE
    assert all(s["status"] == "DONE" for s in result["stories"])
