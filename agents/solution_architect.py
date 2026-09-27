"""Solution Architect Agent -- graph node `solution_architect_agent`.

Spec: `docs/agents/02-solution-architect-agent.md`.

Decides HOW the backlog is built, never redefines WHAT. Analyzes the *entire*
backlog as a whole and attaches architecture, API design, DB design, per-story
technical dependencies and adaptive scope flags, then requests human approval.

Re-entered with `human_approval.comments` populated when a human REJECTS the
design -- on re-entry it revises rather than regenerating from scratch.

Reads:  epics, stories, human_approval.comments (re-entry only)
Writes: architecture_doc_ref, dev_plan_ref, stories[].technical_dependencies,
        stories[].requires_ui, stories[].requires_api, stories[].status="REFINED",
        human_approval.status="PENDING"
Tools:  Jira only -- never touches the repository.
"""

from __future__ import annotations

import logging

from agents import (
    AgentOutputError,
    as_bool,
    as_str_list,
    detect_cycle,
    invoke_agent_json,
)
from core.prompts import SOLUTION_ARCHITECT_SYSTEM_PROMPT
from core.state import ApprovalState, StoryRef, WorkflowState
from tools import get_jira

logger = logging.getLogger(__name__)

AGENT_NAME = "solution_architect_agent"


def solution_architect_node(state: WorkflowState) -> dict:
    """Run the Solution Architect and return a partial state update.

    Must not mark any story READY -- readiness additionally requires human
    approval, which this agent only requests.
    """
    stories = state["stories"]
    if not stories:
        raise AgentOutputError("solution_architect_agent invoked with an empty backlog")

    approval = state["human_approval"]
    is_revision = approval["status"] == "REJECTED"
    if is_revision:
        logger.info(
            "[%s] revision re-entry; incorporating rejection comments", AGENT_NAME
        )

    context: dict = {
        "requirement": state["requirement"],
        "epics": state["epics"],
        # Business fields only -- read-only to this agent.
        "stories": [
            {
                "id": story["id"],
                "epic_id": story["epic_id"],
                "title": story["title"],
                "priority": story["priority"],
                "business_dependencies": story["business_dependencies"],
            }
            for story in stories
        ],
    }
    if is_revision:
        context["human_approval.comments"] = approval["comments"]
        context["previous_architecture_doc_ref"] = state["architecture_doc_ref"]
        context["previous_dev_plan_ref"] = state["dev_plan_ref"]

    known_ids = {story["id"] for story in stories}
    # Spelling the ids out separately from the story objects makes them harder
    # for a small model to overlook -- llama3.1:8b otherwise tends to echo the
    # placeholder ids from the schema example instead of the real Jira keys.
    context["story_ids_to_refine"] = sorted(known_ids)

    def _validate(candidate: dict) -> None:
        """Reject a well-formed answer that refines the wrong or too few stories.

        Raised inside the invoke loop so the model gets the specific fault back
        and can correct it, rather than the whole run halting on one bad reply.
        """
        raw = candidate.get("stories")
        if not isinstance(raw, list) or not raw:
            raise AgentOutputError("output must contain a non-empty 'stories' list")
        seen = {
            str(item.get("id", "")).strip()
            for item in raw
            if isinstance(item, dict) and str(item.get("id", "")).strip()
        }
        # Every story must be refined; an unrefined story could never become
        # READY and would silently strand the backlog.
        missing = known_ids - seen
        if missing:
            raise AgentOutputError(
                f"these story ids are missing from 'stories': {sorted(missing)}. "
                f"Refine exactly these ids, copied verbatim: {sorted(known_ids)}"
            )
        for field in ("architecture_document", "development_plan"):
            if not str(candidate.get(field) or "").strip():
                raise AgentOutputError(
                    f"{field!r} is empty; a story cannot be REFINED without both "
                    "the architecture document and the development plan"
                )
        unknown = seen - known_ids
        if unknown:
            raise AgentOutputError(
                f"these ids are not in the backlog: {sorted(unknown)}. "
                f"Use exactly these ids, copied verbatim: {sorted(known_ids)}"
            )

    # The schema example is built from this run's real ids rather than the
    # generic placeholders. A small model reliably copies whatever ids the
    # example shows, so the example is made to already be the right answer.
    story_entries = ",\n".join(
        f'    {{\n'
        f'      "id": "{story_id}",\n'
        f'      "technical_dependencies": [],\n'
        f'      "requires_ui": false,\n'
        f'      "requires_api": true,\n'
        f'      "status": "REFINED"\n'
        f'    }}'
        for story_id in sorted(known_ids)
    )
    schema = f"""{{
  "architecture_document": "# System Architecture\\n\\nMarkdown: service boundaries, layering, whether a frontend is needed, API endpoints and contracts, database schema.",
  "development_plan": "# Development Plan\\n\\nMarkdown: the build sequence implied by the dependency graph.",
  "stories": [
{story_entries}
  ],
  "human_approval_requested": true
}}

The "stories" array above already lists every id you must refine, in the right
shape. Keep those ids exactly as they are; fill in the technical fields.
"technical_dependencies" may only contain ids from that same list: no dangling
ids, no self-reference, no cycles.
If the requirement is backend-only, "requires_ui" MUST be false on every story."""

    result = invoke_agent_json(
        AGENT_NAME,
        SOLUTION_ARCHITECT_SYSTEM_PROMPT,
        context,
        # The most constrained output in the system: it must echo back every
        # story id verbatim, so it gets an extra correction round before the
        # run halts.
        attempts=3,
        validate=_validate,
        schema=schema,
    )

    refinements = {
        str(raw.get("id", "")).strip(): raw
        for raw in result["stories"]
        if isinstance(raw, dict) and str(raw.get("id", "")).strip()
    }

    refined: list[StoryRef] = []
    for story in stories:
        raw = refinements[story["id"]]

        # Spec section 6: no dangling references, no self-reference.
        technical_dependencies = [
            dep
            for dep in as_str_list(raw.get("technical_dependencies"))
            if dep in known_ids and dep != story["id"]
        ]
        dropped = set(as_str_list(raw.get("technical_dependencies"))) - set(
            technical_dependencies
        )
        if dropped:
            logger.warning(
                "[%s] %s: dropped unresolvable technical_dependencies %s",
                AGENT_NAME,
                story["id"],
                sorted(dropped),
            )

        updated = StoryRef(
            # Business fields carried through untouched -- read-only to this agent.
            id=story["id"],
            epic_id=story["epic_id"],
            title=story["title"],
            priority=story["priority"],
            business_dependencies=story["business_dependencies"],
            # Technical fields, this agent's own.
            technical_dependencies=technical_dependencies,
            requires_ui=as_bool(raw.get("requires_ui"), default=False),
            requires_api=as_bool(raw.get("requires_api"), default=True),
            status="REFINED",
        )
        refined.append(updated)
        logger.info(
            "[%s] %s REFINED | requires_ui=%s requires_api=%s technical_dependencies=%s",
            AGENT_NAME,
            updated["id"],
            updated["requires_ui"],
            updated["requires_api"],
            updated["technical_dependencies"],
        )

    # `select_ready_story` waits on business AND technical dependencies together,
    # so the cycle that strands the backlog can be formed across the two kinds --
    # neither list is cyclic on its own. Check the union, and resolve it by
    # dropping technical edges, which are this agent's to give up; the business
    # ordering belongs to the PM and is left intact.
    while True:
        cycle = detect_cycle(
            {
                story["id"]: list(story["technical_dependencies"])
                + list(story["business_dependencies"])
                for story in refined
            }
        )
        if not cycle:
            break
        source, target = cycle[-2], cycle[-1]
        culprit = next(
            (
                story
                for story in refined
                if story["id"] == source and target in story["technical_dependencies"]
            ),
            None,
        )
        if culprit is None:
            # Every edge in the cycle is a business dependency, which this agent
            # must not rewrite. The PM breaks those at source, so reaching here
            # means the backlog itself is unbuildable -- say so instead of
            # letting the run report "backlog complete" with nothing built.
            raise AgentOutputError(
                "the backlog contains a circular business dependency that no story "
                "can satisfy: " + " -> ".join(cycle)
            )
        culprit["technical_dependencies"] = [
            dep for dep in culprit["technical_dependencies"] if dep != target
        ]
        logger.warning(
            "[%s] broke a dependency cycle: dropped technical %s -> %s (cycle %s)",
            AGENT_NAME,
            source,
            target,
            " -> ".join(cycle),
        )

    architecture_document = str(result.get("architecture_document") or "").strip()
    development_plan = str(result.get("development_plan") or "").strip()
    if not architecture_document or not development_plan:
        # Spec section 6: a story cannot be REFINED without an architecture
        # reference; the Developer and Code Reviewer both read these documents.
        raise AgentOutputError(
            "solution_architect_agent must return both architecture_document and "
            "development_plan content"
        )

    # Jira holds the documents; state holds only the refs it hands back. The
    # attachments land on the design issue (the first epic by default).
    jira = get_jira()
    epic_key = state["epics"][0]["id"] if state["epics"] else None
    architecture_doc_ref = jira.attach_architecture_doc(architecture_document, epic_key)
    dev_plan_ref = jira.attach_dev_plan(development_plan, epic_key)
    logger.info(
        "[%s] attached design docs (%s, %s)", AGENT_NAME, architecture_doc_ref, dev_plan_ref
    )

    # Push the technical refinement back onto each Jira story. Links are created
    # here too, so Jira's dependency graph matches the one state carries.
    for story in refined:
        jira.update_story(
            story_id=story["id"],
            technical_dependencies=story["technical_dependencies"],
            requires_ui=story["requires_ui"],
            requires_api=story["requires_api"],
            status="REFINED",
        )

    return {
        "architecture_doc_ref": architecture_doc_ref,
        "dev_plan_ref": dev_plan_ref,
        "stories": refined,
        # Requests approval; never grants it (spec section 9).
        "human_approval": ApprovalState(
            status="PENDING",
            approved_by=None,
            approved_at=None,
            comments=approval["comments"] if is_revision else None,
        ),
    }


__all__ = ["AGENT_NAME", "solution_architect_node"]
