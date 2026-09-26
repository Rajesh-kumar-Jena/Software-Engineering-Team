"""HTTP API that runs the workflow and streams its progress live.

The dashboard posts a requirement here, then watches the run happen over
Server-Sent Events. Nothing is replayed or simulated: events come from the
LangGraph stream and from the agents' own log records as they execute.

    python server.py            # http://localhost:8000

Endpoints
    POST /api/runs                  start a run      -> {run_id}
    GET  /api/runs/{id}/events      SSE event stream
    GET  /api/runs/{id}             current snapshot of the run
    POST /api/runs/{id}/decision    approve or reject at the human gate
    GET  /api/config                what the UI should show about this install

One run at a time. The workflow writes to real Jira and GitHub, so allowing
concurrent runs would interleave two backlogs in one project -- and the log
handler that captures agent output is global, so their event streams would mix.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import tools
from config import settings
from export_snapshot import build_snapshot
from core.graph import DEFAULT_RECURSION_LIMIT, build_graph
from core.state import create_initial_state

logger = logging.getLogger(__name__)

#: Loggers whose records are surfaced to the UI. Anything else (httpx chatter,
#: provider SDK retries) stays in the server log where it belongs.
_WATCHED = ("agents", "core.graph", "tools.jira_client", "tools.github_client", "tools.testing_tools")

app = FastAPI(title="Autonomous SE Team API")
app.add_middleware(
    CORSMiddleware,
    # The dashboard is served from Vite in development and from any static host
    # in production; this API is local-only and carries no credentials of its own.
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Run:
    """One workflow execution and everything the UI needs to render it."""

    id: str
    requirement: str
    dry_run: bool
    status: str = "running"  # running | awaiting_approval | done | halted | error
    events: list[dict] = field(default_factory=list)
    queue: "queue.Queue[dict]" = field(default_factory=queue.Queue)
    state: dict = field(default_factory=dict)
    awaiting: dict | None = None
    thread: threading.Thread | None = None
    seq: int = 0

    def emit(self, event: dict) -> None:
        # Every event carries a sequence number so a subscriber can replay the
        # history and then join the live queue without seeing anything twice --
        # the two sources overlap for whatever was emitted before it connected.
        self.seq += 1
        event = {"at": _now(), "seq": self.seq, **event}
        self.events.append(event)
        self.queue.put(event)


RUNS: dict[str, Run] = {}
_ACTIVE: Run | None = None
_LOCK = threading.Lock()


class _RunLogHandler(logging.Handler):
    """Forwards agent/tool log records to the run that is currently executing.

    The agents already narrate what they are doing through `logging`; rather
    than duplicating that as bespoke callbacks, this turns those same records
    into UI events, so the dashboard shows exactly what the CLI shows.
    """

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        run = _ACTIVE
        if run is None or not record.name.startswith(_WATCHED):
            return
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 -- never let logging break a run
            return
        run.emit(
            {
                "type": "log",
                "level": record.levelname.lower(),
                "source": record.name,
                "message": message[:500],
            }
        )


_handler = _RunLogHandler()
_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(_handler)
logging.getLogger().setLevel(logging.INFO)
for noisy in ("httpx", "urllib3", "google_genai.models", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


def _summarize(state: dict) -> dict:
    """The slice of WorkflowState the dashboard renders."""
    pr = state.get("pr")
    return {
        "workflow_status": str(state.get("workflow_status") or ""),
        "current_story_id": state.get("current_story_id"),
        "halt_reason": state.get("halt_reason"),
        "counters": {
            "dev_attempts": state.get("dev_attempts", 0),
            "review_iterations": state.get("review_iterations", 0),
            "qa_iterations": state.get("qa_iterations", 0),
        },
        "epics": state.get("epics", []),
        "stories": state.get("stories", []),
        "review_findings": state.get("review_findings", []),
        "review_status": state.get("review_status"),
        "qa_status": state.get("qa_status"),
        "branch_name": state.get("branch_name"),
        "pr": pr,
        "error_log": state.get("error_log", []),
    }


def _pending_interrupt(graph, config) -> dict | None:
    """The payload the graph is suspended on, if it is suspended.

    Asking the checkpointer is authoritative: whether an `__interrupt__` chunk
    also appears in the stream depends on the stream mode and the LangGraph
    version, and missing it would leave the UI waiting forever on a run that is
    actually paused for the human gate.
    """
    try:
        snapshot = graph.get_state(config)
    except Exception:  # noqa: BLE001 -- treat an unreadable checkpoint as "not paused"
        return None
    for task in getattr(snapshot, "tasks", ()) or ():
        for interrupt in getattr(task, "interrupts", ()) or ():
            value = getattr(interrupt, "value", interrupt)
            if isinstance(value, dict):
                return value
    return None


def _drive(run: Run, graph, config, first_input: Any) -> None:
    """Stream the graph, turning each node completion into an event."""
    for chunk in graph.stream(first_input, config, stream_mode="updates"):
        for node, update in chunk.items():
            if node == "__interrupt__":
                payload = update[0].value if hasattr(update[0], "value") else update[0]
                run.awaiting = payload
                run.status = "awaiting_approval"
                run.emit({"type": "awaiting_approval", "payload": payload})
                return
            if isinstance(update, dict):
                run.state.update(update)
            run.emit({"type": "node", "node": node, "state": _summarize(run.state)})

    # The stream can finish simply because the graph suspended at the human
    # gate, which is not the same as the run being over.
    payload = _pending_interrupt(graph, config)
    if payload is not None:
        run.awaiting = payload
        run.status = "awaiting_approval"
        run.emit({"type": "awaiting_approval", "payload": payload})


def _execute(run: Run, resume: Any = None, config: dict | None = None, graph=None) -> None:
    """Run (or resume) the workflow on a worker thread."""
    global _ACTIVE
    _ACTIVE = run
    try:
        _drive(run, graph, config, resume)
        if run.status != "awaiting_approval":
            status = run.state.get("workflow_status", "")
            run.status = "halted" if str(status) == "HALTED" else "done"
            run.emit({"type": "done", "status": run.status, "state": _summarize(run.state)})
    except Exception as exc:  # noqa: BLE001 -- surface any failure to the UI
        logger.exception("run %s failed", run.id)
        run.status = "error"
        run.emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        _ACTIVE = None
        run.queue.put({"type": "__eof__"})


class StartRequest(BaseModel):
    requirement: str
    dry_run: bool = False


class DecisionRequest(BaseModel):
    approved: bool
    comments: str | None = None
    by: str | None = "dashboard"


@app.get("/api/config")
def get_config() -> dict:
    """What this install is wired to, so the UI can be honest about it."""
    return {
        "repo": f"{settings.GITHUB_REPO_OWNER}/{settings.GITHUB_REPO_NAME}",
        "jira_project": settings.JIRA_PROJECT_KEY,
        "models": [
            {"agent": a, "provider": p, "model": m}
            for a, p, m in settings.describe_agent_models()
        ],
        "limits": dict(settings.LIMITS),
        "tools_dry_run": settings.TOOLS_DRY_RUN,
    }


#: A snapshot read hits Jira and GitHub, so repeated page loads share one for a
#: few seconds. `?refresh=1` forces a fresh read -- what the dashboard uses
#: after a run finishes, when the backlog has certainly changed.
_SNAPSHOT: dict | None = None
_SNAPSHOT_AT = 0.0
_SNAPSHOT_TTL = 20.0
_SNAPSHOT_LOCK = threading.Lock()


@app.get("/api/snapshot")
def get_snapshot(refresh: bool = False) -> dict:
    """The project as it stands now: Jira backlog, pull requests, findings.

    The dashboard reads this instead of a JSON file baked in at build time, so
    clearing the Jira project empties the panels rather than leaving a stale
    backlog on screen.
    """
    global _SNAPSHOT, _SNAPSHOT_AT
    with _SNAPSHOT_LOCK:
        age = time.monotonic() - _SNAPSHOT_AT
        if _SNAPSHOT is not None and not refresh and age < _SNAPSHOT_TTL:
            return _SNAPSHOT
        try:
            _SNAPSHOT = build_snapshot()
            _SNAPSHOT_AT = time.monotonic()
        except Exception as exc:  # noqa: BLE001 -- a stale panel beats a broken page
            logger.warning("snapshot refresh failed: %s", exc)
            if _SNAPSHOT is None:
                raise HTTPException(503, f"could not read Jira/GitHub: {exc}") from exc
        return _SNAPSHOT


@app.post("/api/runs")
def start_run(request: StartRequest) -> dict:
    requirement = request.requirement.strip()
    if not requirement:
        raise HTTPException(400, "requirement must not be empty")

    global _ACTIVE
    with _LOCK:
        # A run suspended at the human gate is still in progress even though no
        # thread is executing it: `_execute` clears _ACTIVE when the graph
        # interrupts. Without this second check a dashboard opened in another
        # tab shows an idle composer and can interleave a second backlog into
        # the same Jira project while the first run waits for approval.
        paused = next((r for r in RUNS.values() if r.status == "awaiting_approval"), None)
        if _ACTIVE is not None:
            raise HTTPException(409, "a run is already in progress")
        if paused is not None:
            raise HTTPException(
                409, f"run {paused.id} is waiting for approval; decide on it before starting another"
            )

        # Per-run tool mode. The clients read this when constructed, so the
        # cache has to be dropped for the change to take effect.
        # Live mode is "auto", not "false": "false" would force EVERY tool real,
        # including SonarQube, which has no credentials and no server -- the
        # Code Reviewer would fail against it. "auto" keeps Jira and GitHub real
        # (they have tokens) and simulates only what cannot authenticate.
        settings.TOOLS_DRY_RUN = "true" if request.dry_run else "auto"
        tools.reset_clients()

        run = Run(id=uuid.uuid4().hex[:12], requirement=requirement, dry_run=request.dry_run)
        RUNS[run.id] = run

        graph = build_graph(checkpointer=InMemorySaver())
        config = {
            "configurable": {"thread_id": run.id},
            "recursion_limit": DEFAULT_RECURSION_LIMIT,
        }
        run._graph = graph  # type: ignore[attr-defined]
        run._config = config  # type: ignore[attr-defined]

        initial = create_initial_state(
            requirement=requirement,
            repo=settings.get_repo_ref(),
            limits=settings.LIMITS,
        )
        run.state = dict(initial)
        # Carry the initial state on the first event so the UI can render its
        # status and counters straight away, rather than staying blank until
        # the first node happens to finish.
        run.emit(
            {
                "type": "started",
                "requirement": requirement,
                "dry_run": request.dry_run,
                "state": _summarize(run.state),
            }
        )

        _ACTIVE = run
        run.thread = threading.Thread(
            target=_execute, args=(run,), kwargs={"resume": initial, "config": config, "graph": graph}, daemon=True
        )
        run.thread.start()

    return {"run_id": run.id}


@app.post("/api/runs/{run_id}/decision")
def decide(run_id: str, request: DecisionRequest) -> dict:
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "unknown run")
    if run.status != "awaiting_approval":
        raise HTTPException(409, f"run is {run.status}, not awaiting approval")

    decision = {
        "status": "APPROVED" if request.approved else "REJECTED",
        "approved_by": request.by,
        "comments": request.comments,
    }
    global _ACTIVE
    run.awaiting = None
    run.status = "running"
    _ACTIVE = run  # claimed here for the same reason as in start_run
    run.emit({"type": "decision", "decision": decision})
    run.thread = threading.Thread(
        target=_execute,
        args=(run,),
        kwargs={
            "resume": Command(resume=decision),
            "config": run._config,  # type: ignore[attr-defined]
            "graph": run._graph,  # type: ignore[attr-defined]
        },
        daemon=True,
    )
    run.thread.start()
    return {"ok": True}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "unknown run")
    return {
        "id": run.id,
        "requirement": run.requirement,
        "status": run.status,
        "dry_run": run.dry_run,
        "awaiting": run.awaiting,
        "state": _summarize(run.state),
        "events": run.events[-300:],
    }


@app.get("/api/runs/{run_id}/events")
def stream_events(run_id: str) -> StreamingResponse:
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "unknown run")

    def generate() -> Iterator[str]:
        # Replay what already happened, so a late or reconnecting client still
        # sees the whole run rather than only what follows its subscription.
        replayed = 0
        for event in list(run.events):
            replayed = max(replayed, event.get("seq", 0))
            yield f"data: {json.dumps(event)}\n\n"
        while True:
            try:
                event = run.queue.get(timeout=20)
            except queue.Empty:
                yield ": keep-alive\n\n"  # keeps proxies from closing the stream
                continue
            if event.get("type") == "__eof__":
                if run.status != "awaiting_approval":
                    break
                continue
            # The replay above and this queue overlap for anything emitted
            # before the client connected; the sequence number settles it.
            if event.get("seq", 0) <= replayed:
                continue
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
