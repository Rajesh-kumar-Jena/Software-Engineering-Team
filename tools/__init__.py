"""Tools of record: GitHub, Jira, SonarQube, Playwright.

This package module holds the plumbing all three clients share -- the tool-retry
policy and the dry-run switch. It lives here rather than in a new module so the
repository layout stays exactly as `docs/agents/README.md` specifies.

Tool-retry policy (docs/agents/00-orchestration-and-routing.md section 3)
------------------------------------------------------------------------
Tool failures are a distinct failure class from validation failures: they retry
the *same* call up to `MAX_TOOL_RETRIES` with no state or story impact, and
exhausting them is a HARD_LIMIT halt with `kind = "TOOL_FAILURE"`. `tool_retries`
resets at the start of every individual tool invocation, which is why the counter
lives inside `with_retries` rather than in the graph -- one call, one budget.

Only transient conditions are retried. A 4xx other than 429 is a deterministic
answer from the server; repeating it just burns the budget.

Dry-run
-------
`dry_run_enabled()` returns True when `TOOLS_DRY_RUN=true`, or -- under the
default `auto` -- when the credentials a client needs are missing. That keeps the
workflow runnable end to end before any third-party token exists. Every faked
mutation logs a WARNING and returns an id prefixed `DRYRUN-`, so a simulated
merge can never be mistaken for a real one.
"""

from __future__ import annotations

import logging
import random
import time
from functools import lru_cache
from typing import Any, Callable, TypeVar

import requests

from config import settings
from config.settings import (
    HTTP_TIMEOUT,
    LIMITS,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

__all__ = [
    "ToolFailure",
    "DRYRUN_PREFIX",
    "dry_run_enabled",
    "with_retries",
    "RestClient",
    "get_jira",
    "get_github",
    "get_sonarqube",
    "get_playwright",
    "get_api_runner",
    "reset_clients",
]

#: Marks every id minted by a dry-run client, so faked artifacts are obvious in
#: state, in logs, and in anything a human later reads.
DRYRUN_PREFIX = "DRYRUN-"

#: Transient HTTP statuses worth repeating: rate limiting and server-side faults.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

_RETRYABLE_EXCEPTIONS = (
    requests.ConnectionError,
    requests.Timeout,
)


class ToolFailure(RuntimeError):
    """A tool call that could not be completed within `MAX_TOOL_RETRIES`.

    The Code Reviewer, Developer and QA agents let this propagate: the graph
    records it as a TOOL_FAILURE hard limit rather than mistaking an outage for
    a genuine review/QA verdict.
    """

    def __init__(self, tool: str, detail: str, attempts: int) -> None:
        super().__init__(f"{tool} failed after {attempts} attempt(s): {detail}")
        self.tool = tool
        self.detail = detail
        self.attempts = attempts


def dry_run_enabled(*required_settings: str) -> bool:
    """Whether this client should simulate instead of calling the real service.

    Args:
        *required_settings: the credential values the client needs. Under the
            default `TOOLS_DRY_RUN=auto`, any empty one turns dry-run on.
    """
    # Read through the module, never `from ... import TOOLS_DRY_RUN`: the
    # dashboard sets the mode per run (server.start_run), and a value imported
    # at module load would keep the flag frozen at whatever .env said at
    # startup -- silently writing to real Jira and GitHub with "simulate" ticked.
    mode = str(settings.TOOLS_DRY_RUN).strip().lower()
    if mode in {"true", "1", "yes", "on"}:
        return True
    if mode in {"false", "0", "no", "off"}:
        return False
    # "auto": simulate only when the client could not authenticate anyway.
    return any(not value for value in required_settings)


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, _RETRYABLE_EXCEPTIONS):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code in _RETRYABLE_STATUS
    return False


def with_retries(
    tool: str,
    call: Callable[[], T],
    retries: int | None = None,
) -> T:
    """Run `call`, repeating it on transient failure up to `MAX_TOOL_RETRIES`.

    The attempt counter starts at zero here -- this is the "`tool_retries` resets
    at the start of every individual tool invocation" rule from the state schema.

    Raises:
        ToolFailure: when the budget is exhausted, or immediately on a
            non-transient error (a 404 or a 403 will not improve on retry).
    """
    budget = LIMITS["MAX_TOOL_RETRIES"] if retries is None else retries
    tool_retries = 0  # resets per invocation, by construction
    last: Exception | None = None

    while tool_retries < budget:
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 -- classified immediately below
            last = exc
            if not _retryable(exc):
                raise ToolFailure(tool, str(exc), tool_retries + 1) from exc

            tool_retries += 1
            if tool_retries >= budget:
                break
            # Exponential backoff with jitter, so a rate-limited service is not
            # hit again in lockstep by every retry.
            delay = min(2.0**tool_retries, 30.0) * (0.5 + random.random() / 2)
            logger.warning(
                "[%s] transient failure (%s); retry %d/%d in %.1fs",
                tool,
                exc,
                tool_retries,
                budget,
                delay,
            )
            time.sleep(delay)

    raise ToolFailure(tool, str(last), tool_retries)


class RestClient:
    """A small authenticated REST wrapper with the tool-retry policy applied.

    Each client composes one of these rather than inheriting, so the HTTP concern
    stays separate from the domain calls the agent specs name.
    """

    def __init__(
        self,
        tool: str,
        base_url: str,
        *,
        auth: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float = HTTP_TIMEOUT,
    ) -> None:
        self.tool = tool
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        if auth is not None:
            self._session.auth = auth
        self._session.headers.update({"Accept": "application/json"})
        if headers:
            self._session.headers.update(headers)

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        data: Any = None,
        files: Any = None,
        headers: dict[str, str] | None = None,
        expect: tuple[int, ...] | None = None,
    ) -> requests.Response:
        """Issue one request, retrying transient failures.

        Args:
            expect: acceptable status codes. Anything else raises, so a caller
                never has to inspect `.status_code` to know it succeeded.
        """
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"

        def call() -> requests.Response:
            response = self._session.request(
                method,
                url,
                json=json,
                params=params,
                data=data,
                files=files,
                headers=headers,
                timeout=self.timeout,
            )
            allowed = expect or tuple(range(200, 300))
            if response.status_code not in allowed:
                # Raise through requests so `_retryable` can read the status and
                # decide whether repeating is worth anything.
                raise requests.HTTPError(
                    f"{method} {url} -> {response.status_code}: {response.text[:500]}",
                    response=response,
                )
            return response

        return with_retries(f"{self.tool}.{method.lower()}", call)

    def get_json(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs).json()

    def post_json(self, path: str, **kwargs: Any) -> Any:
        response = self.request("POST", path, **kwargs)
        return response.json() if response.content else None

    def put_json(self, path: str, **kwargs: Any) -> Any:
        response = self.request("PUT", path, **kwargs)
        return response.json() if response.content else None


# ---------------------------------------------------------------------------
# Client accessors
# ---------------------------------------------------------------------------
# Agent nodes reach their tools through these rather than constructing a client
# per invocation: a graph loops through the same node many times, and each
# construction otherwise opens a fresh connection pool and re-runs the dry-run
# probe. Imports are deferred to keep this module free of cycles (each client
# module imports RestClient from here).


@lru_cache(maxsize=1)
def get_jira():
    """Shared `JiraClient` -- PM, Solution Architect, Developer, QA."""
    from tools.jira_client import JiraClient

    return JiraClient()


@lru_cache(maxsize=1)
def get_github():
    """Shared `GitHubClient` -- Developer (write), Code Reviewer (read + merge)."""
    from tools.github_client import GitHubClient

    return GitHubClient()


@lru_cache(maxsize=1)
def get_sonarqube():
    """Shared `SonarQubeClient` -- Code Reviewer only."""
    from tools.testing_tools import SonarQubeClient

    return SonarQubeClient()


@lru_cache(maxsize=1)
def get_playwright():
    """Shared `PlaywrightRunner` -- QA only, and only when `requires_ui`."""
    from tools.testing_tools import PlaywrightRunner

    return PlaywrightRunner()


@lru_cache(maxsize=1)
def get_api_runner():
    """Shared `ApiTestRunner` -- QA API, functional and regression tests."""
    from tools.testing_tools import ApiTestRunner

    return ApiTestRunner()


def reset_clients() -> None:
    """Drop every cached client, so settings changes take effect.

    Used by the tests, which flip credentials and dry-run mode between cases.
    """
    for accessor in (get_jira, get_github, get_sonarqube, get_playwright, get_api_runner):
        accessor.cache_clear()
