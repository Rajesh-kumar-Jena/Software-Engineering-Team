"""GitHub tool of record -- branches, commits, pull requests, merges.

Used by the Developer Agent (write) and the Code Reviewer Agent (read + merge).
GitHub is authoritative: `WorkflowState` stores only PR/branch pointers, so every
caller re-reads live data here before acting.

Transport is the GitHub REST API over a bearer token. Commits go through the Git
Data API (blobs -> tree -> commit -> ref update) rather than a local clone, so no
working copy or git binary is needed and every write is atomic at the ref.

Tool failures are transient-retryable up to `MAX_TOOL_RETRIES`
(docs/agents/00-orchestration-and-routing.md section 3); exhausting them is a
HARD_LIMIT halt with `kind = "TOOL_FAILURE"`.

Dry-run: without `GITHUB_TOKEN` / owner / name this client simulates the
repository in memory. Phase 4 was asked for Jira and SonarQube fallbacks only --
GitHub is included as well because without it the workflow cannot be exercised
end to end at all, which is what the fallbacks exist for. Every simulated
mutation logs a WARNING and every minted id carries the `DRYRUN-` prefix, so a
faked merge can never be read as a real one.
"""

from __future__ import annotations

import base64
import itertools
import logging
import time
from typing import Any

from config.settings import (
    GITHUB_API_URL,
    GITHUB_DEFAULT_BRANCH,
    GITHUB_REPO_NAME,
    GITHUB_REPO_OWNER,
    GITHUB_TOKEN,
)
from core.state import Finding, PullRequestRef
from tools import DRYRUN_PREFIX, RestClient, ToolFailure, dry_run_enabled

logger = logging.getLogger(__name__)

#: A freshly opened PR is often not mergeable for a second or two while GitHub
#: works out whether it conflicts.
_MERGE_ATTEMPTS = 3
_MERGE_BACKOFF = 2.0

__all__ = ["GitHubClient"]


def _pr_number(pr_id: str) -> str:
    """Extract the PR number from a `PullRequestRef.id`.

    Ids are stored as the bare number, but a dry-run or hand-written id may carry
    a prefix (`PR-`, `GH-`, `DRYRUN-`), so the trailing digits are what count.
    """
    digits = "".join(itertools.takewhile(str.isdigit, reversed(str(pr_id))))[::-1]
    if not digits:
        raise ToolFailure("github", f"cannot read a PR number from {pr_id!r}", 1)
    return digits


def _findings_markdown(findings: list[Finding]) -> str:
    """Render findings as a review body the Developer can act on directly."""
    if not findings:
        return "No blocking findings."
    lines = ["### Code review findings", ""]
    for finding in findings:
        location = f"`{finding['file']}`"
        if finding.get("line"):
            location += f" line {finding['line']}"
        lines.append(
            f"- **{finding['severity']}** ({finding['source']}) {location} — {finding['message']}"
        )
    return "\n".join(lines)


class _DryRunGitHub:
    """In-memory stand-in for the repository, stateful across a run."""

    def __init__(self, owner: str, name: str, default_branch: str) -> None:
        self.owner = owner or "dryrun-owner"
        self.name = name or "dryrun-repo"
        self.default_branch = default_branch
        self.branches: dict[str, list[dict]] = {default_branch: []}
        self.pulls: dict[str, dict] = {}
        self._pr_numbers = itertools.count(1)

    def require_branch(self, branch: str) -> list[dict]:
        if branch not in self.branches:
            raise ToolFailure("github", f"unknown branch {branch!r} in dry-run store", 1)
        return self.branches[branch]

    def require_pull(self, number: str) -> dict:
        pull = self.pulls.get(number)
        if pull is None:
            raise ToolFailure("github", f"unknown PR #{number} in dry-run store", 1)
        return pull


class GitHubClient:
    """Thin wrapper over the GitHub API, scoped to the calls the specs name."""

    def __init__(
        self,
        token: str = GITHUB_TOKEN,
        owner: str = GITHUB_REPO_OWNER,
        name: str = GITHUB_REPO_NAME,
        api_url: str = GITHUB_API_URL,
    ) -> None:
        self.token = token
        self.owner = owner
        self.name = name
        self.api_url = api_url

        self.dry_run = dry_run_enabled(token, owner, name)
        self._dry = (
            _DryRunGitHub(owner, name, GITHUB_DEFAULT_BRANCH) if self.dry_run else None
        )
        self._rest = (
            None
            if self.dry_run
            else RestClient(
                "github",
                f"{api_url.rstrip('/')}/repos/{owner}/{name}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        )

        if self.dry_run:
            logger.warning(
                "[github] DRY RUN -- no GitHub credentials, simulating the repository "
                "in memory; no branch, commit, PR or merge is real"
            )

    # --- internals ---------------------------------------------------------

    def _ensure_base_ref(self, from_ref: str) -> None:
        """Make sure `from_ref` exists so a branch can be cut from it.

        A freshly created GitHub repository has no commits at all, and the Git
        Data API cannot bootstrap one: every call there needs a parent, and
        `GET /git/ref/heads/<default>` answers 409 "Git Repository is empty".
        The Contents API can create that first commit, so an empty repo is
        seeded with a README rather than failing the Developer's very first
        tool call.

        This runs only when the ref genuinely does not exist; an established
        repository never reaches the write.
        """
        assert self._rest is not None
        try:
            self._rest.get_json(f"/git/ref/heads/{from_ref}")
            return
        except ToolFailure as exc:
            # 409 = repository is empty; 404 = this branch does not exist yet.
            if "-> 409" not in exc.detail and "-> 404" not in exc.detail:
                raise

        logger.warning(
            "[github] %s has no commit on %s; creating an initial commit so the "
            "branch can be cut from it",
            f"{self.owner}/{self.name}",
            from_ref,
        )
        content = base64.b64encode(
            f"# {self.name}\n\nInitialised by the Autonomous Software Engineering Team.\n".encode()
        ).decode("ascii")
        self._rest.put_json(
            "/contents/README.md",
            json={
                "message": "chore: initialise repository",
                "content": content,
                "branch": from_ref,
            },
            expect=(200, 201),
        )

    # --- Developer Agent ---------------------------------------------------

    def create_branch(self, name: str, from_ref: str = GITHUB_DEFAULT_BRANCH) -> str:
        """Branch `name` off `from_ref`; returns the head SHA it points at.

        An existing branch is reused rather than treated as an error -- the
        Developer re-enters on the same feature branch for every code-review fix.
        """
        if self._dry is not None:
            if name in self._dry.branches:
                logger.warning("[github:dry-run] branch %s already exists; reusing", name)
            else:
                self._dry.branches[name] = list(self._dry.require_branch(from_ref))
                logger.warning("[github:dry-run] created branch %s from %s", name, from_ref)
            return f"{DRYRUN_PREFIX}sha-{name}"

        assert self._rest is not None
        try:
            existing = self._rest.get_json(f"/git/ref/heads/{name}")
            logger.info("[github] branch %s already exists; reusing", name)
            return existing["object"]["sha"]
        except ToolFailure as exc:
            # 404 = this branch does not exist yet; 409 = the whole repository
            # is empty, so no ref resolves. Both mean "go on and create it".
            # Anything else (auth, rate limit) must not be swallowed into a
            # confusing error from the create call that follows.
            if "-> 404" not in exc.detail and "-> 409" not in exc.detail:
                raise

        self._ensure_base_ref(from_ref)
        base_sha = self._rest.get_json(f"/git/ref/heads/{from_ref}")["object"]["sha"]
        created = self._rest.post_json(
            "/git/refs", json={"ref": f"refs/heads/{name}", "sha": base_sha}
        )
        logger.info("[github] created branch %s from %s", name, from_ref)
        return created["object"]["sha"]

    def commit(self, branch: str, message: str, files: dict[str, str]) -> str:
        """Commit `files` (path -> content) onto `branch`; returns the commit SHA.

        Built through the Git Data API: a blob per file, a tree based on the
        current head, then a commit, then a fast-forward of the ref.
        """
        if not files:
            raise ToolFailure("github", "commit called with no files", 1)

        if self._dry is not None:
            history = self._dry.require_branch(branch)
            sha = f"{DRYRUN_PREFIX}commit-{len(history) + 1}-{branch}"
            history.append({"sha": sha, "message": message, "files": dict(files)})
            logger.warning(
                "[github:dry-run] committed %d file(s) to %s -- %s",
                len(files),
                branch,
                message.splitlines()[0][:80],
            )
            return sha

        assert self._rest is not None
        head_sha = self._rest.get_json(f"/git/ref/heads/{branch}")["object"]["sha"]
        base_tree = self._rest.get_json(f"/git/commits/{head_sha}")["tree"]["sha"]

        tree_entries = []
        for path, content in files.items():
            blob = self._rest.post_json(
                "/git/blobs",
                json={
                    "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                    "encoding": "base64",
                },
            )
            tree_entries.append(
                {"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]}
            )

        tree = self._rest.post_json(
            "/git/trees", json={"base_tree": base_tree, "tree": tree_entries}
        )
        commit = self._rest.post_json(
            "/git/commits",
            json={"message": message, "tree": tree["sha"], "parents": [head_sha]},
        )
        self._rest.request(
            "PATCH", f"/git/refs/heads/{branch}", json={"sha": commit["sha"]}
        )
        logger.info(
            "[github] committed %d file(s) to %s (%s)", len(files), branch, commit["sha"][:8]
        )
        return commit["sha"]

    def push(self, branch: str) -> None:
        """Confirm `branch` is published.

        `commit()` writes straight to the remote through the Git Data API, so
        there is no local work to push. This verifies the ref resolves, which is
        the property the caller actually wants before opening a PR.
        """
        if self._dry is not None:
            self._dry.require_branch(branch)
            logger.warning("[github:dry-run] push %s (no-op)", branch)
            return

        assert self._rest is not None
        self._rest.get_json(f"/git/ref/heads/{branch}")
        logger.debug("[github] %s is published", branch)

    def open_pull_request(
        self, branch: str, title: str, body: str, story_id: str
    ) -> PullRequestRef:
        """Open a PR from `branch` into the default branch, referencing the story."""
        full_body = f"{body}\n\n---\nJira story: {story_id}"

        if self._dry is not None:
            number = str(next(self._dry._pr_numbers))
            pull = {
                "number": number,
                "url": f"https://github.com/{self._dry.owner}/{self._dry.name}/pull/{number}",
                "branch": branch,
                "title": title,
                "state": "open",
                "story_id": story_id,
                "reviews": [],
            }
            self._dry.pulls[number] = pull
            logger.warning("[github:dry-run] opened PR #%s from %s", number, branch)
            return PullRequestRef(
                id=number,
                url=pull["url"],
                story_id=story_id,
                kind="FEATURE",
                status="OPEN",
            )

        assert self._rest is not None
        pull = self._rest.post_json(
            "/pulls",
            json={
                "title": title,
                "body": full_body,
                "head": branch,
                "base": GITHUB_DEFAULT_BRANCH,
            },
        )
        logger.info("[github] opened PR #%s from %s", pull["number"], branch)
        return PullRequestRef(
            id=str(pull["number"]),
            url=pull["html_url"],
            story_id=story_id,
            kind="FEATURE",
            status="OPEN",
        )

    def update_pull_request(
        self, pr_id: str, branch: str, message: str
    ) -> PullRequestRef:
        """Refresh an existing PR after new commits landed on its branch.

        Commits pushed by `commit()` attach to the open PR automatically, so this
        records the update as a PR comment and returns the PR's live state. It
        never opens a second PR -- the spec forbids that for a review fix.
        """
        number = _pr_number(pr_id)

        if self._dry is not None:
            pull = self._dry.require_pull(number)
            pull.setdefault("updates", []).append(message)
            logger.warning("[github:dry-run] updated PR #%s (%s)", number, message[:80])
            return PullRequestRef(
                id=number,
                url=pull["url"],
                story_id=pull["story_id"],
                kind="FEATURE",
                status="OPEN" if pull["state"] == "open" else "MERGED",
            )

        assert self._rest is not None
        self._rest.post_json(
            f"/issues/{number}/comments",
            json={"body": f"Updated in response to review feedback: {message}"},
        )
        pull = self._rest.get_json(f"/pulls/{number}")
        logger.info("[github] updated PR #%s on %s", number, branch)
        return PullRequestRef(
            id=number,
            url=pull["html_url"],
            story_id=str(pull.get("head", {}).get("ref", branch)),
            kind="FEATURE",
            status="MERGED" if pull.get("merged") else "OPEN",
        )

    def list_repository_files(self, ref: str = GITHUB_DEFAULT_BRANCH) -> list[str]:
        """Every tracked file path at `ref`.

        The Developer needs this because it authors whole files: without knowing
        what already exists it silently breaks cross-file contracts -- rewriting
        a module that another file imports from, or re-introducing a framework
        the repository does not use.
        """
        if self._dry is not None:
            paths: list[str] = []
            for commit in self._dry.branches.get(ref, []):
                paths.extend(commit["files"])
            return sorted(set(paths))

        assert self._rest is not None
        try:
            tree = self._rest.get_json(f"/git/trees/{ref}", params={"recursive": "1"})
        except ToolFailure as exc:
            # An empty repository has no tree yet; that is not an error here.
            if "-> 409" in exc.detail or "-> 404" in exc.detail:
                return []
            raise
        return sorted(
            entry["path"] for entry in tree.get("tree", []) if entry.get("type") == "blob"
        )

    def read_repository_file(self, path: str, ref: str = GITHUB_DEFAULT_BRANCH) -> str:
        """Contents of one tracked file at `ref`, or "" if it does not exist."""
        if self._dry is not None:
            content = ""
            for commit in self._dry.branches.get(ref, []):
                if path in commit["files"]:
                    content = commit["files"][path]
            return content

        assert self._rest is not None
        try:
            blob = self._rest.get_json(f"/contents/{path}", params={"ref": ref})
        except ToolFailure as exc:
            if "-> 404" in exc.detail:
                return ""
            raise
        if blob.get("encoding") != "base64":
            return str(blob.get("content", ""))
        return base64.b64decode(blob["content"]).decode("utf-8", errors="replace")

    # --- Code Reviewer Agent -----------------------------------------------

    def get_pull_request_diff(self, pr_id: str) -> str:
        """Return the unified diff for the PR, for the reviewer to read."""
        number = _pr_number(pr_id)

        if self._dry is not None:
            pull = self._dry.require_pull(number)
            history = self._dry.branches.get(pull["branch"], [])
            rendered = [
                f"--- a/{path}\n+++ b/{path}\n{content}"
                for commit in history
                for path, content in commit["files"].items()
            ]
            logger.warning("[github:dry-run] diff for PR #%s (%d file blocks)", number, len(rendered))
            return "\n".join(rendered) or "(no changes recorded in the dry-run store)"

        assert self._rest is not None
        response = self._rest.request(
            "GET", f"/pulls/{number}", headers={"Accept": "application/vnd.github.v3.diff"}
        )
        return response.text

    def post_review_comment(self, pr_id: str, findings: list[Finding]) -> None:
        """Post findings on the PR.

        Tries a review with inline comments first so each finding lands on its
        own line. GitHub rejects a comment whose file/line is not part of the
        diff (422), so on rejection the whole set is posted as one PR comment
        instead -- the Developer still gets every finding, just not inline.
        """
        number = _pr_number(pr_id)
        body = _findings_markdown(findings)

        if self._dry is not None:
            self._dry.require_pull(number).setdefault("reviews", []).append(body)
            logger.warning(
                "[github:dry-run] posted %d finding(s) on PR #%s", len(findings), number
            )
            return

        assert self._rest is not None
        inline = [
            {"path": f["file"], "line": f["line"], "body": f"**{f['severity']}** ({f['source']}) {f['message']}"}
            for f in findings
            if f.get("line")
        ]
        if inline:
            try:
                self._rest.post_json(
                    f"/pulls/{number}/reviews",
                    json={"body": body, "event": "REQUEST_CHANGES", "comments": inline},
                )
                logger.info(
                    "[github] posted %d inline finding(s) on PR #%s", len(inline), number
                )
                return
            except ToolFailure as exc:
                logger.warning(
                    "[github] inline review rejected (%s); posting as a PR comment",
                    exc.detail[:200],
                )

        self._rest.post_json(f"/issues/{number}/comments", json={"body": body})
        logger.info("[github] posted %d finding(s) on PR #%s", len(findings), number)

    def merge_pull_request(self, pr_id: str) -> PullRequestRef:
        """Merge on a Code Reviewer PASS only. Never callable by the Developer."""
        number = _pr_number(pr_id)

        if self._dry is not None:
            pull = self._dry.require_pull(number)
            pull["state"] = "merged"
            self._dry.branches[self._dry.default_branch].extend(
                self._dry.branches.get(pull["branch"], [])
            )
            logger.warning("[github:dry-run] merged PR #%s", number)
            return PullRequestRef(
                id=number,
                url=pull["url"],
                story_id=pull["story_id"],
                kind="FEATURE",
                status="MERGED",
            )

        assert self._rest is not None
        # GitHub computes mergeability asynchronously and answers 405 while that
        # is still pending -- a PR opened moments ago is the normal case here.
        # 405 is not a transient status in general (it means "method not
        # allowed"), so it is retried here rather than in the shared policy.
        for attempt in range(1, _MERGE_ATTEMPTS + 1):
            response = self._rest.request(
                "PUT",
                f"/pulls/{number}/merge",
                json={"merge_method": "squash"},
                expect=(200, 405),
            )
            if response.status_code == 200:
                break
            if attempt == _MERGE_ATTEMPTS:
                raise ToolFailure(
                    "github.merge",
                    f"PR #{number} was still not mergeable after {_MERGE_ATTEMPTS} attempts: "
                    f"{response.text[:200]}",
                    attempt,
                )
            logger.warning(
                "[github] PR #%s not mergeable yet (attempt %d/%d); waiting %.0fs",
                number,
                attempt,
                _MERGE_ATTEMPTS,
                _MERGE_BACKOFF,
            )
            time.sleep(_MERGE_BACKOFF)
        pull = self._rest.get_json(f"/pulls/{number}")
        logger.info("[github] merged PR #%s", number)
        return PullRequestRef(
            id=number,
            url=pull["html_url"],
            story_id=str(pull.get("head", {}).get("ref", "")),
            kind="FEATURE",
            status="MERGED",
        )
