"""Jira tool of record -- backlog, technical refinement, and defects.

Used by the PM Agent (create), the Solution Architect (refine), the Developer
(read + status transitions) and QA (read acceptance criteria, file bugs).

Jira is authoritative for story content. In particular, acceptance criteria are
NOT carried in `WorkflowState` -- the QA Agent re-reads the originals here so it
tests against the PM's requirements rather than any downstream reinterpretation.

Transport is the Atlassian Cloud REST API v3 over Basic auth (account email +
API token). v3 takes and returns rich text as Atlassian Document Format, so
`_adf_doc` / `_adf_to_text` convert at the boundary and the agents only ever see
plain strings.

Dry-run: without `JIRA_BASE_URL` / `JIRA_API_TOKEN` / `JIRA_USER_EMAIL` this
client simulates Jira in memory (see `_DryRunJira`). The simulation is stateful
on purpose -- criteria the PM writes must still be readable by QA several nodes
later, or the dry-run would not exercise the workflow it exists to test.
"""

from __future__ import annotations

import itertools
import logging
import re
from typing import Any

from requests.auth import HTTPBasicAuth

from config.settings import (
    JIRA_ACCEPTANCE_CRITERIA_FIELD,
    JIRA_API_TOKEN,
    JIRA_BASE_URL,
    JIRA_BUG_ISSUE_TYPE,
    JIRA_DESIGN_ISSUE_KEY,
    JIRA_EPIC_ISSUE_TYPE,
    JIRA_PROJECT_KEY,
    JIRA_STATUS_MAP,
    JIRA_STORY_ISSUE_TYPE,
    JIRA_USER_EMAIL,
)
from core.state import EpicRef, StoryRef, StoryStatus
from tools import DRYRUN_PREFIX, RestClient, ToolFailure, dry_run_enabled

logger = logging.getLogger(__name__)

__all__ = ["JiraClient"]

#: Acceptance criteria live inside the description when no custom field is
#: configured. The markers make the block machine-readable on the way back out.
_AC_HEADER = "--- ACCEPTANCE CRITERIA (managed by the PM Agent, do not edit) ---"
_AC_FOOTER = "--- END ACCEPTANCE CRITERIA ---"

#: Jira priority names by our numeric rank (lower number = higher priority).
_PRIORITY_NAMES = {1: "Highest", 2: "High", 3: "Medium", 4: "Low", 5: "Lowest"}


# ---------------------------------------------------------------------------
# Atlassian Document Format
# ---------------------------------------------------------------------------


def _adf_doc(text: str) -> dict:
    """Wrap plain text as an ADF document, one paragraph per line."""
    paragraphs = []
    for line in (text or "").split("\n"):
        if line.strip():
            paragraphs.append(
                {"type": "paragraph", "content": [{"type": "text", "text": line}]}
            )
        else:
            paragraphs.append({"type": "paragraph", "content": []})
    return {"type": "doc", "version": 1, "content": paragraphs or [{"type": "paragraph"}]}


def _adf_to_text(node: Any) -> str:
    """Flatten an ADF document (or a plain string) back to text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "\n".join(_adf_to_text(item) for item in node)
    if not isinstance(node, dict):
        return str(node)

    if node.get("type") == "text":
        return node.get("text", "")
    children = node.get("content", [])
    joiner = "\n" if node.get("type") in {"doc", "paragraph", "listItem"} else ""
    rendered = joiner.join(_adf_to_text(child) for child in children)
    return rendered


def _embed_criteria(description: str, criteria: list[str]) -> str:
    """Append the managed acceptance-criteria block to a description."""
    lines = [description.strip(), "", _AC_HEADER]
    lines.extend(f"- {item}" for item in criteria)
    lines.append(_AC_FOOTER)
    return "\n".join(lines)


def _extract_criteria(description: str) -> list[str]:
    """Pull the managed block back out; returns [] when it is absent."""
    match = re.search(
        re.escape(_AC_HEADER) + r"(.*?)" + re.escape(_AC_FOOTER),
        description or "",
        re.DOTALL,
    )
    if not match:
        return []
    return [
        line.lstrip("- ").strip()
        for line in match.group(1).strip().split("\n")
        if line.strip().lstrip("- ").strip()
    ]


# ---------------------------------------------------------------------------
# Dry-run simulation
# ---------------------------------------------------------------------------


class _DryRunJira:
    """In-memory stand-in for Jira, stateful across a whole workflow run.

    Mints keys in the configured project's namespace so ids look real, but every
    one carries the `DRYRUN-` marker inside the summary log line and no network
    call is made.
    """

    def __init__(self, project_key: str) -> None:
        self.project_key = project_key
        self._counter = itertools.count(1)
        self.issues: dict[str, dict] = {}
        self.links: list[tuple[str, str, str]] = []
        self.attachments: dict[str, str] = {}

    def new_key(self) -> str:
        return f"{self.project_key}-{next(self._counter)}"

    def create(self, issue_type: str, **fields: Any) -> str:
        key = self.new_key()
        self.issues[key] = {"key": key, "issuetype": issue_type, **fields}
        logger.warning(
            "[jira:dry-run] created %s %s -- %s",
            issue_type,
            key,
            fields.get("summary", ""),
        )
        return key

    def require(self, key: str) -> dict:
        issue = self.issues.get(key)
        if issue is None:
            raise ToolFailure("jira", f"unknown issue {key!r} in dry-run store", 1)
        return issue


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class JiraClient:
    """Thin wrapper over the Jira API, scoped to the calls the specs name."""

    def __init__(
        self,
        base_url: str = JIRA_BASE_URL,
        email: str = JIRA_USER_EMAIL,
        token: str = JIRA_API_TOKEN,
        project_key: str = JIRA_PROJECT_KEY,
    ) -> None:
        self.base_url = base_url
        self.email = email
        self.token = token
        self.project_key = project_key

        self.dry_run = dry_run_enabled(base_url, email, token)
        self._dry = _DryRunJira(project_key) if self.dry_run else None
        self._rest = (
            None
            if self.dry_run
            else RestClient(
                "jira",
                f"{base_url.rstrip('/')}/rest/api/3",
                auth=HTTPBasicAuth(email, token),
                headers={"Content-Type": "application/json"},
            )
        )
        #: First epic created this run; the default home for design attachments.
        self._design_issue_key: str | None = JIRA_DESIGN_ISSUE_KEY or None

        if self.dry_run:
            logger.warning(
                "[jira] DRY RUN -- no Jira credentials, simulating the backlog in memory"
            )

    # --- internals ---------------------------------------------------------

    def _create_issue(self, issue_type: str, fields: dict) -> str:
        """POST an issue, retrying once without fields the project rejects.

        Priority and parent are configured per project template; a project that
        does not expose them answers 400 rather than ignoring them, so the
        offending fields are dropped and the issue is still created.
        """
        assert self._rest is not None
        payload = {
            "fields": {
                "project": {"key": self.project_key},
                "issuetype": {"name": issue_type},
                **fields,
            }
        }
        try:
            return self._rest.post_json("/issue", json=payload)["key"]
        except ToolFailure as exc:
            optional = [f for f in ("priority", "parent") if f in payload["fields"]]
            if not optional:
                raise
            logger.warning(
                "[jira] create %s rejected (%s); retrying without %s",
                issue_type,
                exc.detail[:200],
                optional,
            )
            for field in optional:
                payload["fields"].pop(field, None)
            return self._rest.post_json("/issue", json=payload)["key"]

    def _get_issue(self, key: str) -> dict:
        assert self._rest is not None
        return self._rest.get_json(f"/issue/{key}")

    def _link(self, story_id: str, depends_on: str, note: str) -> None:
        """Record that `story_id` is blocked by `depends_on`."""
        if self._dry is not None:
            self._dry.require(story_id)
            self._dry.require(depends_on)
            self._dry.links.append((story_id, depends_on, note))
            logger.warning("[jira:dry-run] linked %s blocked by %s (%s)", story_id, depends_on, note)
            return

        assert self._rest is not None
        self._rest.request(
            "POST",
            "/issueLink",
            json={
                "type": {"name": "Blocks"},
                # inward issue is blocked by the outward one
                "inwardIssue": {"key": story_id},
                "outwardIssue": {"key": depends_on},
                "comment": {"body": _adf_doc(note)},
            },
            expect=(200, 201, 204),
        )
        logger.info("[jira] %s blocked by %s (%s)", story_id, depends_on, note)

    def _attach(self, filename: str, content: str, issue_key: str | None) -> str:
        """Attach a text document to an issue; returns `<issue>/attachment/<id>`."""
        target = issue_key or self._design_issue_key
        if not target:
            raise ToolFailure(
                "jira",
                "no issue to attach to -- create an epic first or set JIRA_DESIGN_ISSUE_KEY",
                1,
            )

        if self._dry is not None:
            ref = f"{DRYRUN_PREFIX}{target}/attachment/{filename}"
            self._dry.attachments[ref] = content
            logger.warning("[jira:dry-run] attached %s to %s", filename, target)
            return ref

        assert self._rest is not None
        response = self._rest.request(
            "POST",
            f"/issue/{target}/attachments",
            files={"file": (filename, content.encode("utf-8"), "text/markdown")},
            # Atlassian requires this header and rejects a JSON content type here.
            headers={"X-Atlassian-Token": "no-check", "Content-Type": None},  # type: ignore[dict-item]
        )
        attachment_id = response.json()[0]["id"]
        logger.info("[jira] attached %s to %s", filename, target)
        return f"{target}/attachment/{attachment_id}"

    # --- PM Agent ----------------------------------------------------------

    def create_epic(self, title: str) -> EpicRef:
        if self._dry is not None:
            key = self._dry.create(JIRA_EPIC_ISSUE_TYPE, summary=title)
        else:
            key = self._create_issue(JIRA_EPIC_ISSUE_TYPE, {"summary": title})
            logger.info("[jira] created epic %s -- %s", key, title)

        if self._design_issue_key is None:
            self._design_issue_key = key
        return EpicRef(id=key, title=title)

    def create_story(
        self,
        epic_id: str,
        title: str,
        description: str,
        acceptance_criteria: list[str],
        priority: int,
    ) -> StoryRef:
        """Create a Story under `epic_id`, carrying its acceptance criteria.

        The criteria go to the configured custom field when there is one, and
        otherwise into a marked block in the description -- either way
        `read_story_acceptance_criteria` gets the originals back.
        """
        if self._dry is not None:
            key = self._dry.create(
                JIRA_STORY_ISSUE_TYPE,
                summary=title,
                description=description,
                acceptance_criteria=list(acceptance_criteria),
                priority=priority,
                parent=epic_id,
                status="BACKLOG",
            )
        else:
            fields: dict[str, Any] = {"summary": title, "parent": {"key": epic_id}}
            if JIRA_ACCEPTANCE_CRITERIA_FIELD:
                fields["description"] = _adf_doc(description)
                fields[JIRA_ACCEPTANCE_CRITERIA_FIELD] = _adf_doc(
                    "\n".join(f"- {item}" for item in acceptance_criteria)
                )
            else:
                fields["description"] = _adf_doc(
                    _embed_criteria(description, acceptance_criteria)
                )
            if priority in _PRIORITY_NAMES:
                fields["priority"] = {"name": _PRIORITY_NAMES[priority]}

            key = self._create_issue(JIRA_STORY_ISSUE_TYPE, fields)
            logger.info("[jira] created story %s (P%s) -- %s", key, priority, title)

        return StoryRef(
            id=key,
            epic_id=epic_id,
            title=title,
            priority=priority,
            business_dependencies=[],
            technical_dependencies=[],
            requires_ui=False,
            requires_api=False,
            status="BACKLOG",
        )

    def link_dependency(self, story_id: str, depends_on_story_id: str) -> None:
        """Business dependency (PM-owned)."""
        self._link(story_id, depends_on_story_id, "Business dependency (PM Agent)")

    # --- Solution Architect Agent ------------------------------------------

    def attach_architecture_doc(self, content: str, issue_key: str | None = None) -> str:
        return self._attach("architecture.md", content, issue_key)

    def attach_dev_plan(self, content: str, issue_key: str | None = None) -> str:
        return self._attach("development-plan.md", content, issue_key)

    def update_story(
        self,
        story_id: str,
        technical_dependencies: list[str] | None = None,
        requires_ui: bool | None = None,
        requires_api: bool | None = None,
        status: StoryStatus | None = None,
    ) -> StoryRef:
        """Write the Architect's technical fields onto an existing story.

        Jira has no native home for the adaptive scope flags, so they are
        recorded as labels (`requires-ui` / `backend-only`), which are queryable
        and survive round-tripping. Dependencies become issue links; `status`
        goes through the workflow transition endpoint.
        """
        if self._dry is not None:
            issue = self._dry.require(story_id)
            if technical_dependencies is not None:
                issue["technical_dependencies"] = list(technical_dependencies)
            if requires_ui is not None:
                issue["requires_ui"] = requires_ui
            if requires_api is not None:
                issue["requires_api"] = requires_api
            if status is not None:
                issue["status"] = status
            logger.warning("[jira:dry-run] updated %s", story_id)
        else:
            assert self._rest is not None
            labels = []
            if requires_ui is not None:
                labels.append("requires-ui" if requires_ui else "backend-only")
            if requires_api:
                labels.append("requires-api")
            if labels:
                self._rest.request(
                    "PUT",
                    f"/issue/{story_id}",
                    json={"update": {"labels": [{"add": label} for label in labels]}},
                    expect=(200, 204),
                )
            logger.info("[jira] updated %s labels=%s", story_id, labels)

        for dependency in technical_dependencies or []:
            self.link_technical_dependency(story_id, dependency)
        if status is not None:
            self.transition_story_status(story_id, status)

        return self.read_story(story_id)  # type: ignore[return-value]

    def link_technical_dependency(self, story_id: str, depends_on_story_id: str) -> None:
        """Technical dependency (Architect-owned; additive to the business one)."""
        self._link(
            story_id, depends_on_story_id, "Technical dependency (Solution Architect Agent)"
        )

    # --- Developer Agent ---------------------------------------------------

    def read_story(self, story_id: str) -> dict:
        """Return the live story: summary, description, criteria, status, priority."""
        if self._dry is not None:
            issue = self._dry.require(story_id)
            return {
                "id": story_id,
                "title": issue.get("summary", ""),
                "description": issue.get("description", ""),
                "acceptance_criteria": list(issue.get("acceptance_criteria", [])),
                "status": issue.get("status", "BACKLOG"),
                "priority": issue.get("priority"),
                "labels": issue.get("labels", []),
            }

        raw = self._get_issue(story_id)
        fields = raw.get("fields", {})
        description = _adf_to_text(fields.get("description"))
        return {
            "id": raw.get("key", story_id),
            "title": fields.get("summary", ""),
            "description": description,
            "acceptance_criteria": self.read_story_acceptance_criteria(story_id),
            "status": (fields.get("status") or {}).get("name", ""),
            "priority": (fields.get("priority") or {}).get("name"),
            "labels": fields.get("labels", []),
        }

    def read_architecture_doc(self, ref: str) -> str:
        """Fetch attachment content by the ref `attach_*` returned."""
        if self._dry is not None:
            content = self._dry.attachments.get(ref)
            if content is None:
                raise ToolFailure("jira", f"unknown attachment ref {ref!r}", 1)
            return content

        assert self._rest is not None
        attachment_id = ref.rsplit("/", 1)[-1]
        # The content endpoint sits outside /rest/api/3 and redirects to storage.
        response = self._rest.request(
            "GET",
            f"{self.base_url.rstrip('/')}/rest/api/3/attachment/content/{attachment_id}",
            expect=(200, 206),
        )
        return response.text

    def transition_story_status(self, story_id: str, status: StoryStatus) -> None:
        """Move the issue through its workflow to the mapped status.

        Jira workflows are project-specific, so an unmapped or unavailable
        transition is logged and skipped: the run's own `WorkflowState` remains
        the authority on where a story is, and a mismatched Jira board should not
        halt an otherwise healthy workflow.
        """
        target = JIRA_STATUS_MAP.get(status.upper())
        if not target:
            logger.info("[jira] no workflow mapping for %s; leaving %s as is", status, story_id)
            return

        if self._dry is not None:
            self._dry.require(story_id)["status"] = status
            logger.warning("[jira:dry-run] %s -> %s", story_id, status)
            return

        assert self._rest is not None
        available = self._rest.get_json(f"/issue/{story_id}/transitions")["transitions"]
        match = next(
            (t for t in available if t.get("to", {}).get("name", "").lower() == target.lower()),
            None,
        )
        if match is None:
            logger.warning(
                "[jira] %s has no transition to %r (available: %s)",
                story_id,
                target,
                [t.get("to", {}).get("name") for t in available],
            )
            return

        self._rest.request(
            "POST",
            f"/issue/{story_id}/transitions",
            json={"transition": {"id": match["id"]}},
            expect=(200, 204),
        )
        logger.info("[jira] %s -> %s (%s)", story_id, status, target)

    # --- QA Agent ----------------------------------------------------------

    def read_story_acceptance_criteria(self, story_id: str) -> list[str]:
        """The PM's ORIGINAL criteria -- the only thing QA is allowed to test against."""
        if self._dry is not None:
            return list(self._dry.require(story_id).get("acceptance_criteria", []))

        fields = self._get_issue(story_id).get("fields", {})
        if JIRA_ACCEPTANCE_CRITERIA_FIELD:
            raw = fields.get(JIRA_ACCEPTANCE_CRITERIA_FIELD)
            criteria = [
                line.lstrip("- ").strip()
                for line in _adf_to_text(raw).split("\n")
                if line.strip()
            ]
            if criteria:
                return criteria
            logger.warning(
                "[jira] %s is empty on %s; falling back to the description block",
                JIRA_ACCEPTANCE_CRITERIA_FIELD,
                story_id,
            )

        criteria = _extract_criteria(_adf_to_text(fields.get("description")))
        if not criteria:
            logger.warning("[jira] %s has no readable acceptance criteria", story_id)
        return criteria

    def create_bug(self, story_id: str, description: str, evidence: str) -> str:
        """File a QA defect against `story_id` and link it back to the story."""
        summary = f"[QA] {story_id}: {description.strip().splitlines()[0][:180]}"
        body = f"{description}\n\nEvidence:\n{evidence}"

        if self._dry is not None:
            bug_id = self._dry.create(
                JIRA_BUG_ISSUE_TYPE, summary=summary, description=body, status="BACKLOG"
            )
        else:
            bug_id = self._create_issue(
                JIRA_BUG_ISSUE_TYPE, {"summary": summary, "description": _adf_doc(body)}
            )
            logger.info("[jira] filed bug %s against %s", bug_id, story_id)

        self.link_bug_to_story(bug_id, story_id)
        return bug_id

    def link_bug_to_story(self, bug_id: str, story_id: str) -> None:
        """Link a defect to the story it was found on (traceability, spec section 9)."""
        if self._dry is not None:
            self._dry.links.append((bug_id, story_id, "QA defect"))
            logger.warning("[jira:dry-run] linked bug %s to %s", bug_id, story_id)
            return

        assert self._rest is not None
        self._rest.request(
            "POST",
            "/issueLink",
            json={
                "type": {"name": "Relates"},
                "inwardIssue": {"key": bug_id},
                "outwardIssue": {"key": story_id},
                "comment": {"body": _adf_doc("QA defect found against this story")},
            },
            expect=(200, 201, 204),
        )
        logger.info("[jira] linked bug %s to %s", bug_id, story_id)
