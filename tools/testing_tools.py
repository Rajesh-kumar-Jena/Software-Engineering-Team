"""SonarQube and Playwright hooks, plus the API/regression test runner.

Three distinct consumers, deliberately kept apart:

  * `SonarQubeClient`  -- Code Reviewer Agent only (static analysis quality gate).
  * `PlaywrightRunner` -- QA Agent only, and ONLY when the story under test has
    `requires_ui == true`. A backend-only story must never reach it (adaptive
    scope, docs/agents/00-orchestration-and-routing.md section 6).
  * `ApiTestRunner`    -- QA Agent: API, functional and regression tests.

The Developer Agent has no access to anything in this module; its own
self-validation is limited to running the app and unit tests locally.

Spec format
-----------
`ApiTestRunner` and `PlaywrightRunner` read JSON specs from `QA_SPEC_DIR`:

    tests/qa/PROJ-14.json      API cases     -> {"cases": [...]}
    tests/qa/PROJ-14.ui.json   Playwright    -> {"flows": [...]}

Every result maps back to the acceptance criterion it exercises via a `criterion`
field, which is what lets the QA Agent report `failed_criteria` honestly.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from config.settings import (
    QA_BASE_URL,
    QA_SPEC_DIR,
    QA_UI_TIMEOUT,
    SONAR_HOST_URL,
    SONAR_PROJECT_KEY,
    SONAR_SCAN_TIMEOUT,
    SONAR_SCANNER_PATH,
    SONAR_TOKEN,
)
from core.state import Finding
from tools import DRYRUN_PREFIX, RestClient, ToolFailure, dry_run_enabled

logger = logging.getLogger(__name__)

__all__ = ["SonarQubeClient", "PlaywrightRunner", "ApiTestRunner"]

#: SonarQube severities mapped onto the schema's four levels. Sonar's CRITICAL
#: sits above MAJOR and below BLOCKER; it is folded into BLOCKER so it gates the
#: merge, which is the safer direction for a severity this system has no slot for.
_SONAR_SEVERITY = {
    "BLOCKER": "BLOCKER",
    "CRITICAL": "BLOCKER",
    "MAJOR": "MAJOR",
    "MINOR": "MINOR",
    "INFO": "INFO",
}

#: Findings at these levels fail the gate (docs/agents/04-code-reviewer-agent.md
#: section 6). Kept local to the tool so it can decide without importing the
#: reviewer's logic; the agent applies the same rule to its own manual findings.
_BLOCKING = frozenset({"BLOCKER", "MAJOR"})


# ---------------------------------------------------------------------------
# SonarQube
# ---------------------------------------------------------------------------


class SonarQubeClient:
    """Static analysis quality gate -- Code Reviewer Agent only.

    The Web API cannot start an analysis, so `trigger_scan` shells out to
    `sonar-scanner` and reads the Compute Engine task id out of the report file
    the scanner writes. `get_quality_gate_result` then waits for that task and
    reads both the gate status and the issues it raised.

    Dry-run (no `SONAR_TOKEN`, or no scanner on PATH) returns a passing gate with
    no findings and logs a warning on every call. That is deliberately the
    permissive direction: it keeps the workflow runnable, but it means the review
    verdict rests on the reviewer's manual findings alone, so a dry-run PASS is
    not evidence that real static analysis would agree.
    """

    def __init__(
        self,
        host_url: str = SONAR_HOST_URL,
        token: str = SONAR_TOKEN,
        project_key: str = SONAR_PROJECT_KEY,
    ) -> None:
        self.host_url = host_url
        self.token = token
        self.project_key = project_key

        scanner_available = shutil.which(SONAR_SCANNER_PATH) is not None
        self.dry_run = dry_run_enabled(host_url, token, project_key) or not scanner_available
        self._rest = (
            None
            if self.dry_run
            # Sonar takes the token as the basic-auth username with no password.
            else RestClient("sonarqube", host_url, auth=(token, ""))
        )

        if self.dry_run:
            reason = (
                "no SonarQube credentials"
                if scanner_available
                else f"{SONAR_SCANNER_PATH!r} not found on PATH"
            )
            logger.warning(
                "[sonarqube] DRY RUN -- %s; the quality gate will not actually run", reason
            )

    def trigger_scan(self, branch: str) -> str:
        """Start a scan of `branch`; returns the Compute Engine task id."""
        if self.dry_run:
            task_id = f"{DRYRUN_PREFIX}scan-{branch}"
            logger.warning("[sonarqube:dry-run] pretended to scan %s", branch)
            return task_id

        command = [
            SONAR_SCANNER_PATH,
            f"-Dsonar.projectKey={self.project_key}",
            f"-Dsonar.host.url={self.host_url}",
            f"-Dsonar.token={self.token}",
            f"-Dsonar.branch.name={branch}",
        ]
        logger.info("[sonarqube] scanning %s", branch)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=SONAR_SCAN_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToolFailure("sonarqube.scan", str(exc), 1) from exc

        if completed.returncode != 0:
            raise ToolFailure(
                "sonarqube.scan",
                f"scanner exited {completed.returncode}: {completed.stderr[-1000:]}",
                1,
            )

        # The scanner records the task id in .scannerwork/report-task.txt; the
        # stdout fallback covers a non-default working directory.
        report = Path(".scannerwork/report-task.txt")
        if report.is_file():
            for line in report.read_text(encoding="utf-8").splitlines():
                if line.startswith("ceTaskId="):
                    return line.split("=", 1)[1].strip()

        match = re.search(r"ceTaskId=(\S+)", completed.stdout)
        if match:
            return match.group(1)
        raise ToolFailure("sonarqube.scan", "scanner did not report a ceTaskId", 1)

    def get_quality_gate_result(self, scan_id: str) -> tuple[bool, list[Finding]]:
        """Return `(gate_passed, findings)` with findings tagged source=SONARQUBE.

        The gate passes only when Sonar's own status is OK *and* no BLOCKER or
        MAJOR issue was raised -- the second condition is this system's rule, and
        it is applied here so a project with a lenient gate profile still cannot
        wave a blocker through.
        """
        if self.dry_run:
            logger.warning(
                "[sonarqube:dry-run] reporting a passing gate for %s with no findings",
                scan_id,
            )
            return True, []

        assert self._rest is not None
        analysis_id = self._await_analysis(scan_id)
        status = self._rest.get_json(
            "/api/qualitygates/project_status", params={"analysisId": analysis_id}
        )
        gate_ok = status.get("projectStatus", {}).get("status") == "OK"

        findings = self._fetch_issues()
        blocking = [f for f in findings if f["severity"] in _BLOCKING]
        passed = gate_ok and not blocking

        logger.info(
            "[sonarqube] gate=%s, %d finding(s), %d blocking -> %s",
            "OK" if gate_ok else "FAILED",
            len(findings),
            len(blocking),
            "PASS" if passed else "FAIL",
        )
        return passed, findings

    def _await_analysis(self, task_id: str) -> str:
        """Poll the Compute Engine task until it produces an analysis id."""
        assert self._rest is not None
        deadline = time.monotonic() + SONAR_SCAN_TIMEOUT
        while time.monotonic() < deadline:
            task = self._rest.get_json("/api/ce/task", params={"id": task_id})["task"]
            state = task.get("status")
            if state == "SUCCESS":
                return task["analysisId"]
            if state in {"FAILED", "CANCELED"}:
                raise ToolFailure("sonarqube.task", f"analysis {state.lower()}", 1)
            time.sleep(5)
        raise ToolFailure(
            "sonarqube.task", f"analysis did not finish within {SONAR_SCAN_TIMEOUT}s", 1
        )

    def _fetch_issues(self) -> list[Finding]:
        """Read open issues for the project, paging until they are all in."""
        assert self._rest is not None
        findings: list[Finding] = []
        page = 1
        while True:
            payload = self._rest.get_json(
                "/api/issues/search",
                params={
                    "componentKeys": self.project_key,
                    "resolved": "false",
                    "ps": 500,
                    "p": page,
                },
            )
            for issue in payload.get("issues", []):
                # component is "projectKey:path/to/file"
                component = issue.get("component", "")
                findings.append(
                    Finding(
                        source="SONARQUBE",
                        severity=_SONAR_SEVERITY.get(issue.get("severity", ""), "MAJOR"),  # type: ignore[typeddict-item]
                        file=component.split(":", 1)[-1] or component,
                        line=issue.get("line"),
                        message=issue.get("message", ""),
                    )
                )
            paging = payload.get("paging", {})
            if page * paging.get("pageSize", 500) >= paging.get("total", 0):
                return findings
            page += 1


# ---------------------------------------------------------------------------
# Spec loading (shared by both QA runners)
# ---------------------------------------------------------------------------


def _load_spec(spec: str | dict, suffix: str) -> dict:
    """Resolve a spec argument into a dict.

    Accepts an already-parsed dict, a path to a JSON file, or a story id whose
    spec is looked up at `QA_SPEC_DIR/<story-id><suffix>`. Returns `{}` when
    nothing is found -- a missing spec is reported as a skip by the caller, not
    as a passing test.
    """
    if isinstance(spec, dict):
        return spec

    candidates = [Path(spec), Path(QA_SPEC_DIR) / f"{spec}{suffix}"]
    for candidate in candidates:
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ToolFailure("qa.spec", f"{candidate} is not valid JSON: {exc}", 1) from exc

    logger.warning("[qa] no spec found for %r (looked in %s)", spec, [str(c) for c in candidates])
    return {}


# ---------------------------------------------------------------------------
# Playwright
# ---------------------------------------------------------------------------


class PlaywrightRunner:
    """UI test execution -- QA Agent only, and only when `requires_ui` is true.

    A flow is a named list of steps against the running build::

        {"flows": [{"name": "create note", "criterion": "Given ... Then ...",
                    "steps": [{"goto": "/notes"},
                              {"fill": "#title", "value": "hello"},
                              {"click": "text=Save"},
                              {"expect_text": "hello"}]}]}

    A failing step captures a screenshot next to the spec, so the Jira bug QA
    files carries real evidence rather than a bare assertion message.
    """

    _STEP_KEYS = ("goto", "click", "fill", "expect_text", "expect_visible", "wait_for")

    def __init__(self, base_url: str | None = None) -> None:
        # Resolved at construction rather than bound as a default at import, so
        # the QA target can differ per environment (and per test).
        self.base_url = (base_url or QA_BASE_URL).rstrip("/")

    @staticmethod
    def available() -> tuple[bool, str]:
        """Whether Playwright and a browser binary are both usable."""
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError:
            return False, "playwright is not installed (pip install playwright)"
        return True, ""

    def run_ui_tests(self, spec: str | dict) -> dict:
        """Execute UI flows; returns `{passed, failures, results, evidence}`."""
        ok, reason = self.available()
        if not ok:
            raise ToolFailure("playwright", reason, 1)

        loaded = _load_spec(spec, ".ui.json")
        flows = loaded.get("flows", [])
        if not flows:
            logger.warning("[playwright] no UI flows defined for %r -- nothing to run", spec)
            return {"passed": True, "skipped": True, "failures": [], "results": [], "evidence": []}

        from playwright.sync_api import Error as PlaywrightError, sync_playwright

        results: list[dict] = []
        failures: list[dict] = []
        evidence: list[str] = []

        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PlaywrightError as exc:
                raise ToolFailure(
                    "playwright",
                    f"could not launch chromium ({exc}); run `playwright install chromium`",
                    1,
                ) from exc

            try:
                for flow in flows:
                    name = flow.get("name", "unnamed flow")
                    criterion = flow.get("criterion", name)
                    page = browser.new_page()
                    page.set_default_timeout(QA_UI_TIMEOUT * 1000)
                    console_errors: list[str] = []
                    page.on(
                        "console",
                        lambda msg: console_errors.append(msg.text)
                        if msg.type == "error"
                        else None,
                    )
                    try:
                        self._run_steps(page, flow.get("steps", []))
                        results.append({"name": name, "criterion": criterion, "passed": True})
                        logger.info("[playwright] %s PASSED", name)
                    except Exception as exc:  # noqa: BLE001 -- any step failure is a test failure
                        safe_name = re.sub(r"\W+", "-", name).strip("-") or "flow"
                        shot = Path(QA_SPEC_DIR) / f"failure-{safe_name}.png"
                        try:
                            shot.parent.mkdir(parents=True, exist_ok=True)
                            page.screenshot(path=str(shot))
                            evidence.append(str(shot))
                        except Exception:  # noqa: BLE001 -- evidence is best-effort
                            logger.debug("[playwright] could not capture a screenshot")
                        failure = {
                            "name": name,
                            "criterion": criterion,
                            "error": str(exc),
                            "console_errors": console_errors[:20],
                        }
                        failures.append(failure)
                        results.append({**failure, "passed": False})
                        logger.warning("[playwright] %s FAILED: %s", name, exc)
                    finally:
                        page.close()
            finally:
                browser.close()

        return {
            "passed": not failures,
            "skipped": False,
            "failures": failures,
            "results": results,
            "evidence": evidence,
        }

    def _run_steps(self, page: Any, steps: list[dict]) -> None:
        for step in steps:
            if "goto" in step:
                target = step["goto"]
                page.goto(target if target.startswith("http") else self.base_url + target)
            elif "click" in step:
                page.click(step["click"])
            elif "fill" in step:
                page.fill(step["fill"], step.get("value", ""))
            elif "wait_for" in step:
                page.wait_for_selector(step["wait_for"])
            elif "expect_text" in step:
                expected = step["expect_text"]
                if expected not in page.content():
                    raise AssertionError(f"expected text {expected!r} not found on the page")
            elif "expect_visible" in step:
                if not page.is_visible(step["expect_visible"]):
                    raise AssertionError(f"{step['expect_visible']!r} is not visible")
            else:
                raise ToolFailure(
                    "playwright",
                    f"unrecognised step {step!r}; supported keys: {self._STEP_KEYS}",
                    1,
                )


# ---------------------------------------------------------------------------
# API / regression
# ---------------------------------------------------------------------------


class ApiTestRunner:
    """API / functional / regression test execution -- QA Agent.

    A case is one HTTP request plus its expectations::

        {"cases": [{"name": "create note", "criterion": "Given ... Then ...",
                    "method": "POST", "path": "/notes",
                    "json": {"title": "hi"},
                    "expect_status": 201,
                    "expect_json": {"title": "hi"},
                    "expect_body_contains": "hi"}]}
    """

    def __init__(self, base_url: str | None = None) -> None:
        # Resolved at construction rather than bound as a default at import, so
        # the QA target can differ per environment (and per test).
        self.base_url = (base_url or QA_BASE_URL).rstrip("/")

    def run(self, spec: str | dict) -> dict:
        """Execute API cases; returns `{passed, failures, results}`."""
        loaded = _load_spec(spec, ".json")
        cases = loaded.get("cases", [])
        if not cases:
            logger.warning("[api-tests] no cases defined for %r -- nothing to run", spec)
            return {"passed": True, "skipped": True, "failures": [], "results": []}

        results: list[dict] = []
        failures: list[dict] = []

        with httpx.Client(base_url=self.base_url, timeout=QA_UI_TIMEOUT) as client:
            for case in cases:
                name = case.get("name", f"{case.get('method', 'GET')} {case.get('path', '/')}")
                criterion = case.get("criterion", name)
                try:
                    problems = self._run_case(client, case)
                except httpx.HTTPError as exc:
                    problems = [f"request failed: {exc}"]

                if problems:
                    failure = {"name": name, "criterion": criterion, "problems": problems}
                    failures.append(failure)
                    results.append({**failure, "passed": False})
                    logger.warning("[api-tests] %s FAILED: %s", name, "; ".join(problems))
                else:
                    results.append({"name": name, "criterion": criterion, "passed": True})
                    logger.info("[api-tests] %s PASSED", name)

        return {"passed": not failures, "skipped": False, "failures": failures, "results": results}

    def _run_case(self, client: httpx.Client, case: dict) -> list[str]:
        """Run one case; returns the list of unmet expectations (empty = pass)."""
        response = client.request(
            case.get("method", "GET").upper(),
            case.get("path", "/"),
            json=case.get("json"),
            params=case.get("params"),
            headers=case.get("headers"),
        )

        problems: list[str] = []
        expected_status = case.get("expect_status")
        if expected_status is not None and response.status_code != expected_status:
            problems.append(f"expected status {expected_status}, got {response.status_code}")

        contains = case.get("expect_body_contains")
        if contains and contains not in response.text:
            problems.append(f"body does not contain {contains!r}")

        expected_json = case.get("expect_json")
        if expected_json is not None:
            try:
                payload = response.json()
            except ValueError:
                problems.append("response is not JSON")
            else:
                for key, value in expected_json.items():
                    actual = payload.get(key) if isinstance(payload, dict) else None
                    if actual != value:
                        problems.append(f"{key}: expected {value!r}, got {actual!r}")

        return problems

    def run_regression(self, done_story_ids: list[str]) -> dict:
        """Confirm the merge did not break previously DONE stories.

        Re-runs each finished story's own API spec. Stories with no spec on disk
        are reported as skipped rather than silently counted as passing.
        """
        if not done_story_ids:
            return {"passed": True, "skipped": True, "failures": [], "results": [], "covered": []}

        failures: list[dict] = []
        results: list[dict] = []
        covered: list[str] = []
        skipped: list[str] = []

        for story_id in done_story_ids:
            outcome = self.run(story_id)
            if outcome.get("skipped"):
                skipped.append(story_id)
                continue
            covered.append(story_id)
            for item in outcome["results"]:
                results.append({**item, "story_id": story_id})
            for failure in outcome["failures"]:
                failures.append({**failure, "story_id": story_id})

        if skipped:
            logger.warning("[regression] no spec for %s -- not covered by this run", skipped)
        logger.info(
            "[regression] %d story/stories covered, %d failure(s)", len(covered), len(failures)
        )
        return {
            "passed": not failures,
            "skipped": not covered,
            "failures": failures,
            "results": results,
            "covered": covered,
            "not_covered": skipped,
        }
