"""Agent implementations. One module per agent spec in docs/agents/.

This package module also holds the plumbing every agent node shares: building the
system+context message pair, invoking the local model, and parsing its response
into a dict. It lives here rather than in a new module so the repository layout
stays exactly as `docs/agents/README.md` specifies.

Nothing here makes a routing decision -- agents report their local judgment and
LangGraph decides where execution goes next (`core/graph.py`).
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable

import httpx
from langchain_core.messages import HumanMessage, SystemMessage

from config.settings import AGENT_MODELS, get_llm
from core.prompts import OUTPUT_SCHEMAS

logger = logging.getLogger(__name__)

__all__ = [
    "AgentOutputError",
    "invoke_agent_json",
    "detect_cycle",
    "response_text",
    "as_bool",
    "as_int",
    "as_str_list",
    "find_story",
    "story_index",
    "replace_story",
]

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

#: Transport-level faults from the local model server. A timeout here is the
#: expected outcome of a runaway generation (see LLM_TIMEOUT), so it is treated
#: as a retryable attempt rather than allowed to escape: an exception raised
#: through a graph node would abort the run instead of halting it safely.
_LLM_TRANSPORT_ERRORS = (httpx.HTTPError, httpx.TimeoutException, ConnectionError, OSError)

#: Conditions where the provider is unable to serve us *right now* but the
#: request itself is fine: throttling, overload, and server-side faults.
#:
#: Classified by transience rather than by exception type. Enumerating provider
#: exception classes was tried and failed twice in testing -- a 429 slipped
#: through, then a 503 slipped through the fix for the 429 -- because every
#: provider names these differently and adds new ones. Matching on the HTTP
#: semantics in the class name and message covers Gemini, Groq and Ollama alike
#: without importing any of them.
_TRANSIENT_MARKERS = (
    # throttling / quota
    "ratelimit", "rate limit", "resource_exhausted", "resourceexhausted", "429",
    # capacity / availability
    "unavailable", "overload", "high demand", "503", "502", "504",
    # server-side faults and deadlines
    "internal error", "500", "deadline", "timeout", "timed out",
)


def _is_transient(exc: Exception) -> bool:
    """Whether `exc` means "try again shortly" rather than "this request is wrong".

    A throttle or an overloaded backend is operational, not a defect in the
    model's answer: it deserves a backoff and another attempt, and -- when it is
    finally fatal -- a message naming the condition rather than a traceback that
    reads like a bug in this codebase.
    """
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


#: Markers for a provider rejecting the model's own generation as malformed.
#: Groq validates `response_format: json_object` server-side and answers 400
#: `json_validate_failed` with an empty `failed_generation`, so nothing ever
#: reaches the JSON parser here. That is bad model output, not a transport
#: fault: repeating the identical request is pointless, but reprompting with the
#: reason is exactly what fixes it -- the same treatment a JSONDecodeError gets.
_BAD_OUTPUT_MARKERS = (
    "json_validate_failed",
    "failed to validate json",
    "failed_generation",
)


def _is_bad_output(exc: Exception) -> bool:
    """Whether the provider rejected what the model produced, not our request."""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _BAD_OUTPUT_MARKERS)


#: Providers name the exhausted window inside the 429 body, in prose ("per day")
#: or as a quota metric ("...requests_per_model_per_day"). Matching either shape
#: matters: waiting out a per-MINUTE limit works, waiting out a per-DAY one just
#: spends two more sleeps before failing anyway.
_DAILY_QUOTA_PATTERN = re.compile(r"per[\s_\-]*day|daily\s+(?:limit|quota)", re.IGNORECASE)


def _is_daily_quota(exc: Exception) -> bool:
    """Whether this refusal is a daily cap rather than a momentary one."""
    return bool(_DAILY_QUOTA_PATTERN.search(str(exc)))


def _retry_after_seconds(exc: Exception, default: float) -> float:
    """Honour the provider's own `retryDelay` hint when it gives one."""
    match = re.search(r"retry in (\d+(?:\.\d+)?)s", str(exc), re.IGNORECASE)
    if match:
        return min(float(match.group(1)) + 1.0, 120.0)
    return default


def detect_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    """Return one cycle in a dependency graph, or None if it is acyclic.

    Shared by the PM and the Architect because `select_ready_story` waits on the
    UNION of business and technical dependencies: a cycle in either kind, or one
    formed across both, leaves every story permanently unready and ends the run
    as "backlog complete" with nothing built and nothing logged.
    """
    UNVISITED, IN_PROGRESS, DONE = 0, 1, 2
    marks = {node: UNVISITED for node in graph}
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        marks[node] = IN_PROGRESS
        path.append(node)
        for dependency in graph.get(node, []):
            if marks.get(dependency) == IN_PROGRESS:
                return path[path.index(dependency) :] + [dependency]
            if marks.get(dependency, DONE) == UNVISITED:
                found = visit(dependency)
                if found:
                    return found
        path.pop()
        marks[node] = DONE
        return None

    for node in graph:
        if marks[node] == UNVISITED:
            cycle = visit(node)
            if cycle:
                return cycle
    return None



class AgentOutputError(RuntimeError):
    """The model returned something that is not a usable structured object.

    Raised rather than silently defaulted: an agent that cannot produce valid
    structured output must not be allowed to look like a successful run, because
    LangGraph routes on these fields.
    """


def response_text(content: Any) -> str:
    """Flatten a chat model's `content` into plain text.

    Providers disagree on shape: Ollama returns a string, while Gemini returns a
    list of content blocks (`[{"type": "text", "text": "..."}]`). Falling back to
    `str()` on the list yields a Python repr with single quotes, which is not
    JSON and fails to parse -- so the blocks are joined explicitly instead.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content)


def _extract_json(raw: str) -> str:
    """Pull a JSON object out of a model response.

    `get_llm()` requests `format="json"`, so the happy path is that `raw` already
    is a JSON document. The fence and brace fallbacks cover a model that wraps it
    anyway -- llama3.1:8b does this occasionally.
    """
    text = raw.strip()
    if not text:
        raise AgentOutputError("model returned an empty response")

    fenced = _FENCE_RE.search(text)
    if fenced:
        return fenced.group(1).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]

    return text


def invoke_agent_json(
    agent_name: str,
    system_prompt: str,
    context: dict[str, Any],
    attempts: int = 3,
    validate: Callable[[dict[str, Any]], None] | None = None,
    schema: str | None = None,
) -> dict[str, Any]:
    """Invoke the local model with an agent's system prompt and return parsed JSON.

    Args:
        agent_name: graph node name. Selects the output schema and labels logs.
        system_prompt: the agent's template from `core.prompts` (role, duties,
            constraints, expected output shape). Passed as the system message.
        context: the runtime slice of `WorkflowState` this agent is allowed to
            read, per its spec's "Inputs" section. Serialized as the human
            message so the prompt itself stays static and cacheable.
        attempts: how many times to ask. Three by default: a reply can be
            rejected for several independent reasons -- unparseable JSON, a
            provider-side generation rejection, a failed semantic check, a
            transient outage -- and one spare attempt is far cheaper than
            halting a run that has already written to Jira and GitHub. A
            rejected response is retried with the reason fed back -- a small local model drops a required key or
            copies a placeholder id often enough that a single reprompt is worth
            far more than a halt, and these are the fields LangGraph routes on.
        validate: optional semantic check on the parsed object. Raise
            `AgentOutputError` from it to reject a well-formed but wrong
            response (a missing story, a placeholder id); the message is shown
            to the model on the retry, so it can correct the specific fault
            rather than guess. Structural parsing alone cannot catch these.
        schema: overrides the generic `OUTPUT_SCHEMAS` entry. Use it to show the
            model an example built from this call's real data -- a static
            example with placeholder ids invites a small model to copy the
            placeholders verbatim.

    Raises:
        AgentOutputError: when no attempt yields a valid object.
    """
    # Roles differ in how much capability they need; AGENT_MODELS lets the
    # reviewer or architect run on a larger model than the rest.
    model = AGENT_MODELS.get(agent_name)
    llm = get_llm(model=model)
    if model:
        logger.debug("[%s] using model override %s", agent_name, model)

    # The system prompts end with "matching the schema in docs/agents/...", which
    # the model cannot open -- so the schema itself travels with the request.
    schema = schema or OUTPUT_SCHEMAS.get(agent_name)
    instruction = "Return ONLY the JSON object your EXPECTED OUTPUT section defines."
    if schema:
        instruction += "\n\nEXPECTED OUTPUT SCHEMA:\n" + schema

    user_content = (
        "AVAILABLE CONTEXT (JSON):\n"
        + json.dumps(context, indent=2, default=str)
        + "\n\n"
        + instruction
    )
    messages: list = [SystemMessage(content=system_prompt), HumanMessage(content=user_content)]

    last_error = ""
    for attempt in range(1, attempts + 1):
        logger.info("[%s] invoking local model (attempt %d/%d)", agent_name, attempt, attempts)
        response = None
        try:
            # Inside the try: a timeout or a dropped connection raises here, and
            # it must be caught rather than escaping through the graph node.
            try:
                response = llm.invoke(messages)
            except Exception as exc:  # noqa: BLE001 -- translated, then re-raised
                # Translate "the provider rejected the generation" into the same
                # failure the JSON parser would have raised, so it takes the
                # reprompt path instead of looking like an infrastructure fault.
                if _is_bad_output(exc):
                    raise AgentOutputError(
                        f"the provider rejected the model's output as invalid JSON: {exc}"
                    ) from exc
                raise
            raw = response_text(response.content)
            parsed = json.loads(_extract_json(raw))
            if not isinstance(parsed, dict):
                raise AgentOutputError(
                    f"expected a JSON object, got a {type(parsed).__name__}"
                )
            if validate is not None:
                validate(parsed)
            logger.debug("[%s] parsed keys: %s", agent_name, sorted(parsed))
            return parsed
        except (json.JSONDecodeError, AgentOutputError) as exc:
            # MUST stay above the broad `except Exception` below. Ordering here
            # is load-bearing: with the broad clause first, this one becomes
            # unreachable and every reprompt path dies silently -- malformed
            # JSON, the PM's granularity nudge, the Architect's id check.
            last_error = str(exc)
            logger.warning("[%s] unusable response: %s", agent_name, last_error)
            if attempt == attempts:
                break
            if response is not None:
                messages.append(response)
            messages.append(
                HumanMessage(
                    content=(
                        f"That response was not usable: {last_error}\n"
                        "Reply with the JSON object alone -- no prose, no code fence, "
                        "every key from the schema present."
                    )
                )
            )
        except Exception as exc:  # noqa: BLE001 -- classified immediately below
            if _is_transient(exc):
                # A daily quota does not reset within a retry window, so the
                # usual back-off would stall the run for minutes and fail anyway.
                # Say what actually happened instead.
                if _is_daily_quota(exc):
                    logger.error(
                        "[%s] the provider's DAILY quota is exhausted -- retrying cannot "
                        "help until it resets; halting now instead of waiting",
                        agent_name,
                    )
                    raise AgentOutputError(
                        f"{agent_name} stopped: the model provider's daily quota is "
                        f"exhausted ({exc}). Switch provider in AGENT_MODELS or wait for "
                        "the quota to reset."
                    ) from exc
                # The provider cannot serve us right now, but the request is
                # fine. Its SDK has usually already retried internally by the
                # time this surfaces, so back off for as long as it asks before
                # spending another attempt.
                delay = _retry_after_seconds(exc, default=30.0)
                last_error = f"model provider unavailable: {exc}"
                if attempt < attempts:
                    logger.warning(
                        "[%s] transient provider error (%s); waiting %.0fs before attempt %d/%d",
                        agent_name,
                        type(exc).__name__,
                        delay,
                        attempt + 1,
                        attempts,
                    )
                    time.sleep(delay)
                else:
                    logger.warning(
                        "[%s] transient provider error (%s) on the final attempt",
                        agent_name,
                        type(exc).__name__,
                    )
                continue
            if not isinstance(exc, _LLM_TRANSPORT_ERRORS):
                raise
            # No response came back at all, so there is nothing to feed back --
            # just try again with the same messages.
            last_error = f"model call failed ({type(exc).__name__}: {exc})"
            logger.warning("[%s] %s", agent_name, last_error)
            continue

    raise AgentOutputError(
        f"{agent_name} produced no valid output in {attempts} attempt(s): {last_error}"
    )


# ---------------------------------------------------------------------------
# Coercion helpers -- a small local model is not reliably type-faithful, and
# every one of these values is something LangGraph later routes on.
# ---------------------------------------------------------------------------


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "y", "1"}:
            return True
        if lowered in {"false", "no", "n", "0"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def as_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def as_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


# ---------------------------------------------------------------------------
# Story-list helpers (`stories` is a list of pointers, keyed by Jira story id)
# ---------------------------------------------------------------------------


def story_index(stories: list[dict], story_id: str | None) -> int:
    if story_id is None:
        return -1
    for index, story in enumerate(stories):
        if story.get("id") == story_id:
            return index
    return -1


def find_story(stories: list[dict], story_id: str | None) -> dict | None:
    index = story_index(stories, story_id)
    return stories[index] if index >= 0 else None


def replace_story(stories: list[dict], updated: dict) -> list[dict]:
    """Return a new list with `updated` swapped in by id (never mutates in place)."""
    return [dict(updated) if s.get("id") == updated.get("id") else dict(s) for s in stories]
