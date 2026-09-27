"""Centralized configuration: environment loading, retry limits, and LLM init.

Everything the system reads from the outside world is resolved here exactly once,
so no agent or tool module calls `os.getenv` directly.

The reasoning model is a **local Ollama** instance -- see `get_llm()`.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

from dotenv import load_dotenv
from langchain_ollama import ChatOllama

from core.state import DEFAULT_LIMITS, RepoRef, RetryLimits

load_dotenv()  # reads .env from the project root; see .env.example


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    if raw in {"true", "1", "yes", "on"}:
        return True
    if raw in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean, got {raw!r}")


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


# ---------------------------------------------------------------------------
# LLM -- local Ollama backend
# ---------------------------------------------------------------------------

OLLAMA_BASE_URL: str = _env("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL: str = _env("OLLAMA_MODEL", "llama3.1:8b")
LLM_TEMPERATURE: float = _env_float("LLM_TEMPERATURE", 0.0)

#: Context window, in tokens. This MUST be set explicitly: Ollama's own default
#: is 2048 regardless of what the model supports, and it truncates silently --
#: the prompt is cut, the model never sees the end of the schema, and the reply
#: comes back missing keys. Every agent prompt here (system + context JSON +
#: schema, and a PR diff for the reviewer) is far larger than 2048 tokens.
#: Raise it toward the model's maximum; lower it if the machine runs out of
#: memory or inference falls back to CPU and gets slow.
OLLAMA_NUM_CTX: int = _env_int("OLLAMA_NUM_CTX", 16384)

#: Generation cap, in tokens. This MUST be bounded. `-1` (Ollama's "until the
#: context is full") let a 3B model in JSON mode generate for 22 minutes
#: straight on one request, saturating six cores and never returning -- an
#: unbounded generation is a worse failure than a truncated one, because a
#: truncated reply is caught by the JSON parser and reprompted while an endless
#: one just hangs. 4096 tokens is roughly 16k characters: ample for one story's
#: files, and it terminates.
OLLAMA_NUM_PREDICT: int = _env_int("OLLAMA_NUM_PREDICT", 4096)

#: Seconds any single model call may take before it is abandoned. ChatOllama has
#: no timeout of its own, so without this a stalled or runaway generation blocks
#: the whole workflow indefinitely with no way to recover.
LLM_TIMEOUT: float = _env_float("LLM_TIMEOUT", 300.0)

#: Per-agent model overrides, as "<node name>=<model tag>" pairs. The roles do
#: not need equal capability: writing a backlog is far easier than judging a
#: diff, and a model too small to review reliably will invent blocking findings
#: the Developer cannot act on, so the review loop never converges and the run
#: halts on MAX_REVIEW_ITERATIONS. Point the reviewer (and the architect, whose
#: output is the most constrained) at a larger model here rather than paying for
#: one on every call.
#:
#:   AGENT_MODELS=code_reviewer_agent=llama3.1:8b,solution_architect_agent=llama3.1:8b
AGENT_MODELS: dict[str, str] = {
    pair.split("=", 1)[0].strip(): pair.split("=", 1)[1].strip()
    for pair in _env("AGENT_MODELS").split(",")
    if "=" in pair
}

#: Credentials for a hosted reasoning model. Unused by the local Ollama backend;
#: present because the README lists it as a required variable.
LLM_API_KEY: str = _env("LLM_API_KEY")

#: Hosted-provider credentials. Only needed for an agent whose AGENT_MODELS entry
#: names that provider; the local backend needs none.
GEMINI_API_KEY: str = _env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY")

#: Tokens Gemini 2.5 models may spend reasoning before answering. Thinking is
#: drawn from the SAME budget as `max_output_tokens`, so a long reasoning pass
#: returns a truncated or empty body -- which these agents see as unparseable
#: JSON and answer with a reprompt, spending the daily quota three times over.
#: 0 disables it; -1 restores Google's dynamic default. Superseded by
#: `thinking_level` on Gemini 3+, where passing it is harmless.
GEMINI_THINKING_BUDGET: int = _env_int("GEMINI_THINKING_BUDGET", 0)

#: Gemini 3+ replaced the token budget with a named level, and IGNORES
#: `thinking_budget` -- so the 2.5-era setting above silently stops applying the
#: moment the model is a 3.x one. "low" keeps enough reasoning for the Developer
#: and Reviewer while leaving the output budget for the answer.
GEMINI_THINKING_LEVEL: str = _env("GEMINI_THINKING_LEVEL", "low")


def _is_gemini_3_plus(name: str) -> bool:
    """Whether this model takes `thinking_level` rather than `thinking_budget`.

    An unrecognised or `-latest` name is treated as new: the aliases follow the
    newest release, and guessing "old" would silently leave thinking unbounded.
    """
    match = re.search(r"gemini-(\d+)", name)
    return int(match.group(1)) >= 3 if match else True
GROQ_API_KEY: str = _env("GROQ_API_KEY")

#: Provider prefixes recognised in a model spec. Ollama tags contain a colon of
#: their own ("qwen2.5-coder:3b"), so a prefix counts as a provider only when it
#: appears here -- otherwise the whole string is an Ollama tag.
_PROVIDERS = frozenset({"ollama", "gemini", "google", "groq"})

#: Cap on a hosted model's reply, mirroring OLLAMA_NUM_PREDICT.
HOSTED_MAX_TOKENS: int = _env_int("HOSTED_MAX_TOKENS", 4096)


#: Graph nodes that call a model, for reporting which one each will use.
_MODEL_USING_AGENTS = (
    "pm_agent",
    "solution_architect_agent",
    "developer_agent",
    "code_reviewer_agent",
    "qa_agent",
)


def describe_agent_models() -> list[tuple[str, str, str]]:
    """Return `(agent, provider, model)` for every model-using node."""
    rows = []
    for agent in _MODEL_USING_AGENTS:
        provider, name = _split_model_spec(AGENT_MODELS.get(agent) or OLLAMA_MODEL)
        rows.append((agent, provider, name))
    return rows


def _split_model_spec(spec: str) -> tuple[str, str]:
    """Split `"gemini:gemini-2.5-flash"` into `("gemini", "gemini-2.5-flash")`.

    A spec with no recognised provider prefix is an Ollama tag, colons and all.
    """
    prefix, separator, rest = spec.partition(":")
    if separator and prefix.strip().lower() in _PROVIDERS:
        return prefix.strip().lower(), rest.strip()
    return "ollama", spec.strip()


@lru_cache(maxsize=None)
def get_llm(
    model: str | None = None,
    temperature: float | None = None,
    fmt: str | None = "json",
):
    """Return the chat model an agent reasons with.

    Local Ollama is the default and needs no credentials. A model spec may name a
    hosted provider instead, which is how a single judgment-heavy agent (the Code
    Reviewer, say) runs on a stronger model than the rest -- see `AGENT_MODELS`.

    Args:
        model: `"<provider>:<model>"`, or a bare Ollama tag. Recognised providers
            are `ollama`, `gemini` (alias `google`) and `groq`. Defaults to
            `OLLAMA_MODEL`.
        temperature: Sampling temperature. Defaults to `LLM_TEMPERATURE` (0.0) --
            these agents must be deterministic enough for LangGraph to route on
            their output.
        fmt: Response format. Defaults to `"json"` because every agent spec
            requires a structured JSON object and forbids free-form prose. Pass
            `None` for an agent that must emit plain text.

    Cached, so agents sharing a spec share one client (and one connection pool).
    """
    provider, name = _split_model_spec(model or OLLAMA_MODEL)
    resolved_temperature = LLM_TEMPERATURE if temperature is None else temperature

    if provider in {"gemini", "google"}:
        if not GEMINI_API_KEY:
            raise RuntimeError(
                f"{name!r} needs a Gemini key: set GEMINI_API_KEY in .env "
                "(get one free at https://aistudio.google.com/apikey)"
            )
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=name,
            google_api_key=GEMINI_API_KEY,
            temperature=resolved_temperature,
            timeout=LLM_TIMEOUT,
            max_output_tokens=HOSTED_MAX_TOKENS,
            # Native JSON enforcement -- the provider guarantees a parseable
            # object, which removes the failure mode the local models kept
            # hitting (prose around the JSON, truncated objects, copied
            # placeholders from the schema example).
            response_mime_type="application/json" if fmt == "json" else None,
            # Thinking tokens come out of `max_output_tokens`, so an unbounded
            # reasoning pass returns a truncated body the agents cannot parse.
            # The two model generations spell this control differently.
            **(
                {"reasoning_effort": GEMINI_THINKING_LEVEL}
                if _is_gemini_3_plus(name)
                else {"thinking_budget": GEMINI_THINKING_BUDGET}
            ),
        )

    if provider == "groq":
        if not GROQ_API_KEY:
            raise RuntimeError(
                f"{name!r} needs a Groq key: set GROQ_API_KEY in .env "
                "(get one free at https://console.groq.com/keys)"
            )
        try:
            from langchain_groq import ChatGroq
        except ImportError as exc:  # optional dependency
            raise RuntimeError(
                "the groq provider needs `pip install langchain-groq`"
            ) from exc

        return ChatGroq(
            model=name,
            api_key=GROQ_API_KEY,
            temperature=resolved_temperature,
            timeout=LLM_TIMEOUT,
            max_tokens=HOSTED_MAX_TOKENS,
            # Groq rejects a json_object response_format unless the word
            # "json" appears somewhere in the messages. Every agent prompt here
            # ends with "Return ONLY the JSON object ...", which satisfies it --
            # a caller passing fmt="json" with its own prompt must do the same.
            model_kwargs=(
                {"response_format": {"type": "json_object"}} if fmt == "json" else {}
            ),
        )

    return ChatOllama(
        base_url=OLLAMA_BASE_URL,
        model=name,
        temperature=resolved_temperature,
        format=fmt,
        num_ctx=OLLAMA_NUM_CTX,
        num_predict=OLLAMA_NUM_PREDICT,
        # ChatOllama exposes no `timeout`; it reaches the underlying ollama
        # client through client_kwargs.
        client_kwargs={"timeout": LLM_TIMEOUT},
    )


# ---------------------------------------------------------------------------
# Retry / iteration hard limits
# docs/agents/00-orchestration-and-routing.md section 3 -- enforced by LangGraph,
# never by the agent whose loop they gate.
# ---------------------------------------------------------------------------

LIMITS: RetryLimits = RetryLimits(
    MAX_DEV_ATTEMPTS=_env_int("MAX_DEV_ATTEMPTS", DEFAULT_LIMITS["MAX_DEV_ATTEMPTS"]),
    MAX_REVIEW_ITERATIONS=_env_int(
        "MAX_REVIEW_ITERATIONS", DEFAULT_LIMITS["MAX_REVIEW_ITERATIONS"]
    ),
    MAX_QA_ITERATIONS=_env_int("MAX_QA_ITERATIONS", DEFAULT_LIMITS["MAX_QA_ITERATIONS"]),
    MAX_TOOL_RETRIES=_env_int("MAX_TOOL_RETRIES", DEFAULT_LIMITS["MAX_TOOL_RETRIES"]),
)


# ---------------------------------------------------------------------------
# GitHub -- Developer Agent (write) and Code Reviewer Agent (read + merge)
# ---------------------------------------------------------------------------

GITHUB_TOKEN: str = _env("GITHUB_TOKEN")
GITHUB_API_URL: str = _env("GITHUB_API_URL", "https://api.github.com")
GITHUB_REPO_OWNER: str = _env("GITHUB_REPO_OWNER")
GITHUB_REPO_NAME: str = _env("GITHUB_REPO_NAME")
GITHUB_DEFAULT_BRANCH: str = _env("GITHUB_DEFAULT_BRANCH", "main")


def get_repo_ref() -> RepoRef:
    """Build the `RepoRef` the initial `WorkflowState` is seeded with."""
    return RepoRef(
        owner=GITHUB_REPO_OWNER,
        name=GITHUB_REPO_NAME,
        default_branch=GITHUB_DEFAULT_BRANCH,
    )


# ---------------------------------------------------------------------------
# Jira -- backlog of record (PM, Solution Architect, Developer, QA)
# ---------------------------------------------------------------------------

JIRA_BASE_URL: str = _env("JIRA_BASE_URL")
JIRA_API_TOKEN: str = _env("JIRA_API_TOKEN")
JIRA_USER_EMAIL: str = _env("JIRA_USER_EMAIL")
JIRA_PROJECT_KEY: str = _env("JIRA_PROJECT_KEY", "PROJ")

#: Issue type names, which vary between Jira project templates.
JIRA_EPIC_ISSUE_TYPE: str = _env("JIRA_EPIC_ISSUE_TYPE", "Epic")
JIRA_STORY_ISSUE_TYPE: str = _env("JIRA_STORY_ISSUE_TYPE", "Story")
JIRA_BUG_ISSUE_TYPE: str = _env("JIRA_BUG_ISSUE_TYPE", "Bug")

#: Custom field id holding acceptance criteria (e.g. "customfield_10036"). When
#: unset, criteria are written into the description under a marked block and
#: parsed back from there -- QA must be able to re-read the PM's originals.
JIRA_ACCEPTANCE_CRITERIA_FIELD: str = _env("JIRA_ACCEPTANCE_CRITERIA_FIELD")

#: Issue that carries the architecture doc and dev plan as attachments. Defaults
#: to the first epic created in the run.
JIRA_DESIGN_ISSUE_KEY: str = _env("JIRA_DESIGN_ISSUE_KEY")

#: Maps this system's StoryStatus values onto the project's workflow status
#: names, as "OUR_STATUS=Their Status" pairs. Unmapped statuses are skipped with
#: a warning rather than failing the run.
JIRA_STATUS_MAP: dict[str, str] = {
    pair.split("=", 1)[0].strip().upper(): pair.split("=", 1)[1].strip()
    for pair in _env(
        "JIRA_STATUS_MAP",
        "IN_PROGRESS=In Progress,IN_REVIEW=In Review,IN_QA=In Review,DONE=Done,BLOCKED=Blocked",
    ).split(",")
    if "=" in pair
}


# ---------------------------------------------------------------------------
# SonarQube -- Code Reviewer Agent quality gate
# ---------------------------------------------------------------------------

SONAR_TOKEN: str = _env("SONAR_TOKEN")
SONAR_HOST_URL: str = _env("SONAR_HOST_URL", "http://localhost:9000")
SONAR_PROJECT_KEY: str = _env("SONAR_PROJECT_KEY")

#: The Web API cannot start an analysis -- the scanner CLI does. This is the
#: executable to invoke; it must be on PATH or given as an absolute path.
SONAR_SCANNER_PATH: str = _env("SONAR_SCANNER_PATH", "sonar-scanner")
SONAR_SCAN_TIMEOUT: float = _env_float("SONAR_SCAN_TIMEOUT", 900.0)


# ---------------------------------------------------------------------------
# QA -- target of the merged build under test
# ---------------------------------------------------------------------------

QA_BASE_URL: str = _env("QA_BASE_URL", "http://localhost:8000")

#: Directory holding QA specs: `<story-id>.json` for API cases and
#: `<story-id>.ui.json` (or a pytest path) for Playwright flows.
QA_SPEC_DIR: str = _env("QA_SPEC_DIR", "tests/qa")

#: Seconds a Playwright flow may take before it is called a failure.
QA_UI_TIMEOUT: float = _env_float("QA_UI_TIMEOUT", 30.0)

#: When a story has no executable acceptance test on disk, QA has no objective
#: evidence. The default refuses to pass what it could not verify; set false to
#: let the model's judgment against the criteria stand in (demos only).
QA_FAIL_WITHOUT_SPECS: bool = _env_bool("QA_FAIL_WITHOUT_SPECS", True)


# ---------------------------------------------------------------------------
# Developer self-validation
# ---------------------------------------------------------------------------

#: Running model-generated code executes untrusted input on this machine, so it
#: is opt-in. With this off, self-validation is a syntax/parse check over the
#: generated files plus the model's own reported judgment -- weaker than the
#: spec's "run the application and unit tests", and deliberately so by default.
DEV_RUN_TESTS: bool = _env_bool("DEV_RUN_TESTS", False)

#: Command used when DEV_RUN_TESTS is on. Runs in the workspace below.
DEV_TEST_COMMAND: str = _env("DEV_TEST_COMMAND", "pytest -q")

#: Where generated files are materialised for that test run.
DEV_WORKSPACE: str = _env("DEV_WORKSPACE", ".dev-workspace")

#: Largest PR diff handed to the reviewer model, in characters.
REVIEW_DIFF_LIMIT: int = _env_int("REVIEW_DIFF_LIMIT", 30000)

#: How much of the existing repository the Developer is shown before it writes.
#: It authors whole files, so without this it cannot know what it must stay
#: compatible with -- which module another file imports from, which framework
#: the codebase already uses -- and each story silently breaks the last one.
#: Bounded because the prompt has to hold it alongside the design and criteria.
DEV_CONTEXT_MAX_FILES: int = _env_int("DEV_CONTEXT_MAX_FILES", 20)
DEV_CONTEXT_MAX_CHARS: int = _env_int("DEV_CONTEXT_MAX_CHARS", 24000)


# ---------------------------------------------------------------------------
# Tool behaviour
# ---------------------------------------------------------------------------

#: "auto" (default) simulates a service only when its credentials are missing;
#: "true" forces simulation everywhere; "false" always calls the real thing and
#: fails loudly without credentials.
TOOLS_DRY_RUN: str = _env("TOOLS_DRY_RUN", "auto")

HTTP_TIMEOUT: float = _env_float("HTTP_TIMEOUT", 30.0)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

#: Credentials required before any tool of record can be reached. The LLM is not
#: listed: the local Ollama backend needs no key.
REQUIRED_FOR_TOOLS: dict[str, str] = {
    "GITHUB_TOKEN": GITHUB_TOKEN,
    "GITHUB_REPO_OWNER": GITHUB_REPO_OWNER,
    "GITHUB_REPO_NAME": GITHUB_REPO_NAME,
    "JIRA_BASE_URL": JIRA_BASE_URL,
    "JIRA_API_TOKEN": JIRA_API_TOKEN,
    "JIRA_USER_EMAIL": JIRA_USER_EMAIL,
    "SONAR_TOKEN": SONAR_TOKEN,
}


def missing_settings() -> list[str]:
    """Names of required settings that are unset or empty."""
    return [name for name, value in REQUIRED_FOR_TOOLS.items() if not value]


def validate_settings(strict: bool = True) -> list[str]:
    """Report (or raise on) unset required settings.

    Args:
        strict: raise `RuntimeError` when anything is missing. Pass `False` to
            get the list back for a warning instead -- useful while only the
            LLM-facing parts of the system are being exercised.
    """
    missing = missing_settings()
    if missing and strict:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill them in."
        )
    return missing
