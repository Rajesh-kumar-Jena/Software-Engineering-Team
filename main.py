"""Entry point: build the initial state and invoke the graph.

    python main.py "Build a REST API for managing customer subscriptions"
    python main.py --sample --auto-approve -v     # end-to-end demonstration run

The run suspends at `human_approval_gate` -- a real LangGraph interrupt -- and
prints the design awaiting sign-off. Resume the same thread with a decision::

    python main.py --resume --thread-id run-1 --approve --by "your name"
    python main.py --resume --thread-id run-1 --reject  --comments "split story 3"

`--auto-approve` supplies that approval automatically and runs straight through
to backlog completion in one process. It exists for demonstration and testing;
it deliberately bypasses the hard human gate, so it is never the default.

`--dry-run` prints the resolved configuration and the initial state without
calling the model or any tool. `TOOLS_DRY_RUN=true` in the environment is the
separate switch that simulates Jira/GitHub/SonarQube while still exercising the
agents and the graph.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import warnings

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from config import settings
from core.graph import DEFAULT_RECURSION_LIMIT, build_graph
from core.state import create_initial_state

#: Interrupts only survive across processes with a durable checkpointer. This
#: module-level saver keeps one process's threads alive; swap in a persistent
#: backend (e.g. SqliteSaver) to resume a run after the process exits.
CHECKPOINTER = InMemorySaver()

#: Deliberately backend-only, so a demonstration run exercises the adaptive-scope
#: path that bypasses UI generation and Playwright entirely.
SAMPLE_REQUIREMENT = (
    "Build a backend REST API for managing customer subscriptions. It must let a "
    "customer create a subscription to a plan, retrieve their current "
    "subscription, and cancel it. No user interface is required."
)


def _dry_run(requirement: str) -> int:
    """Print the resolved configuration and the initial state; call no tools."""
    missing = settings.validate_settings(strict=False)

    print("LLM")
    print(f"  base_url    : {settings.OLLAMA_BASE_URL}")
    print(f"  model       : {settings.OLLAMA_MODEL}")
    print(f"  temperature : {settings.LLM_TEMPERATURE}")

    print("\nRetry limits")
    for key, value in settings.LIMITS.items():
        print(f"  {key:<22}: {value}")

    print("\nTool credentials")
    if missing:
        print("  MISSING: " + ", ".join(missing))
        print(f"  TOOLS_DRY_RUN={settings.TOOLS_DRY_RUN} -- these services will be simulated")
    else:
        print("  all required variables present")

    state = create_initial_state(
        requirement=requirement,
        repo=settings.get_repo_ref(),
        limits=settings.LIMITS,
    )
    print("\nInitial WorkflowState")
    print(json.dumps(state, indent=2, default=str))
    return 0


def _print_tool_modes() -> None:
    """Say up front which services are real and which are simulated."""
    from tools import get_github, get_jira, get_sonarqube

    print("Tool mode:")
    for label, client in (
        ("jira", get_jira()),
        ("github", get_github()),
        ("sonarqube", get_sonarqube()),
    ):
        print(f"  {label:<10}: {'SIMULATED (dry-run)' if client.dry_run else 'LIVE'}")

    print("Models:")
    for agent, provider, name in settings.describe_agent_models():
        where = "local" if provider == "ollama" else f"hosted/{provider}"
        print(f"  {agent:<24}: {name}  ({where})")
    print()


def _summarize(result: dict) -> None:
    """Print the end-of-run picture: status, counters, stories, errors."""
    print("\n=== RUN SUMMARY ===")
    print(f"  workflow_status : {result.get('workflow_status')}")
    if result.get("halt_reason"):
        print(f"  halt_reason     : {result['halt_reason']}")
    print(
        "  counters        : "
        f"dev={result.get('dev_attempts')} "
        f"review={result.get('review_iterations')} "
        f"qa={result.get('qa_iterations')}"
    )
    for story in result.get("stories", []):
        print(
            f"    {story['id']:<12} {story['status']:<12} "
            f"P{story['priority']} ui={story['requires_ui']} api={story['requires_api']} "
            f"-- {story['title']}"
        )
    if result.get("pr"):
        pr = result["pr"]
        print(f"  last PR         : {pr['id']} ({pr['kind']}, {pr['status']}) {pr['url']}")
    for entry in result.get("error_log", []):
        print(f"  error           : [{entry['kind']}] {entry['detail']}")


def _check_llm() -> int:
    """Ping every configured model once, so a bad key fails in seconds.

    Cheaper than discovering a missing credential or a wrong model name partway
    through a live run that has already written to Jira and GitHub.
    """
    from langchain_core.messages import HumanMessage

    from agents import response_text

    seen: dict[str, str] = {}
    failures = 0
    for agent, provider, name in settings.describe_agent_models():
        spec = settings.AGENT_MODELS.get(agent) or settings.OLLAMA_MODEL
        if spec in seen:
            print(f"  {agent:<24}: {name} ({seen[spec]})")
            continue
        try:
            llm = settings.get_llm(model=spec)
            # The word "json" must appear in the messages: Groq rejects a
            # json_object response_format without it, and every real agent
            # prompt says "Return ONLY the JSON object ...".
            reply = llm.invoke(
                [HumanMessage(content='Reply with the JSON object {"ok": true}')]
            )
            text = response_text(reply.content)[:40]
            seen[spec] = "OK"
            print(f"  {agent:<24}: {name} ({provider}) OK -> {text!r}")
        except Exception as exc:  # noqa: BLE001 -- report every provider, not the first
            failures += 1
            seen[spec] = "FAILED"
            print(f"  {agent:<24}: {name} ({provider}) FAILED -> {exc}")
    return 1 if failures else 0


def _report(result: dict) -> int:
    """Print either the pending approval request or the finished state."""
    pending = result.get("__interrupt__")
    if pending:
        payload = pending[0].value if hasattr(pending[0], "value") else pending[0]
        print("\n=== AWAITING HUMAN APPROVAL ===")
        print(json.dumps(payload, indent=2, default=str))
        print(
            "\nResume with:  python main.py --resume --thread-id <id> "
            "--approve --by '<name>'"
        )
        return 0

    _summarize(result)
    return 1 if result.get("halt_reason") else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Autonomous Software Engineering Team -- run the agent workflow."
    )
    parser.add_argument(
        "requirement",
        nargs="?",
        help="the natural-language business requirement (omit with --sample or --resume)",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="use the built-in sample requirement (a backend-only subscriptions API)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print resolved config and the initial state, then exit without running the graph",
    )
    parser.add_argument("--thread-id", default="run-1", help="LangGraph thread id")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume a thread suspended at the human approval gate",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help=(
            "approve the design automatically and run through to backlog completion "
            "in one process (demonstration/testing only -- bypasses the human gate)"
        ),
    )
    parser.add_argument("--approve", action="store_true", help="approve the design")
    parser.add_argument("--reject", action="store_true", help="reject the design")
    parser.add_argument("--by", default=None, help="who is approving/rejecting")
    parser.add_argument("--comments", default=None, help="rejection feedback")
    parser.add_argument(
        "--recursion-limit",
        type=int,
        default=DEFAULT_RECURSION_LIMIT,
        help="LangGraph super-step budget for the backlog loop",
    )
    parser.add_argument(
        "--check-llm",
        action="store_true",
        help="ping every configured model once and exit (validates keys and model names)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log agent activity")
    args = parser.parse_args(argv)

    # Windows consoles default to cp1252, and this system routinely logs text it
    # did not author -- PR titles, diffs, review bodies, model output. A single
    # emoji in any of those raises UnicodeEncodeError from inside the logging
    # handler and takes the run down. Force UTF-8 rather than lose a run to it.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    # Advisory about automatic function calling; fires on every hosted call and
    # is not actionable for this workload.
    logging.getLogger("google_genai.models").setLevel(logging.ERROR)
    # Some Gemini models use fixed sampling and warn that temperature is
    # ignored; it would repeat on every call and needs no action.
    warnings.filterwarnings("ignore", message=".*sampling defaults.*")

    if args.check_llm:
        print("Model check:")
        return _check_llm()

    requirement = SAMPLE_REQUIREMENT if args.sample else args.requirement

    if args.dry_run:
        if not requirement:
            parser.error("--dry-run needs a requirement (or --sample)")
        return _dry_run(requirement)

    config = {
        "configurable": {"thread_id": args.thread_id},
        "recursion_limit": args.recursion_limit,
    }
    graph = build_graph(checkpointer=CHECKPOINTER)

    if args.resume:
        if args.approve == args.reject:
            parser.error("--resume needs exactly one of --approve or --reject")
        decision = {
            "status": "APPROVED" if args.approve else "REJECTED",
            "approved_by": args.by,
            "comments": args.comments,
        }
        return _report(graph.invoke(Command(resume=decision), config))

    if not requirement:
        parser.error("a requirement is required unless --sample or --resume is given")

    missing = settings.validate_settings(strict=False)
    if missing:
        print(
            "note: unset credentials (" + ", ".join(missing) + ")",
            file=sys.stderr,
        )
    _print_tool_modes()

    print(f"Requirement: {requirement}\n")
    initial = create_initial_state(
        requirement=requirement,
        repo=settings.get_repo_ref(),
        limits=settings.LIMITS,
    )
    result = graph.invoke(initial, config)

    if result.get("__interrupt__") and args.auto_approve:
        print("\n[--auto-approve] approving the design and continuing\n")
        result = graph.invoke(
            Command(
                resume={
                    "status": "APPROVED",
                    "approved_by": args.by or "--auto-approve",
                    "comments": "Approved automatically by --auto-approve",
                }
            ),
            config,
        )

    return _report(result)


if __name__ == "__main__":
    sys.exit(main())
