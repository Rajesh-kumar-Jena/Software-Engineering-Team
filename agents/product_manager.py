"""Product Manager Agent -- graph node `pm_agent`.

Spec: `docs/agents/01-product-manager-agent.md`.

Decides WHAT should be built, never HOW. Converts the raw business requirement
into a prioritized, dependency-aware Jira backlog of Epics, Stories, and
Acceptance Criteria.

Reads:  requirement
Writes: epics, stories (status="BACKLOG")
Tools:  Jira only -- no GitHub, SonarQube, or Playwright.

Jira mints the real issue keys, so the ids the model proposes are treated purely
as internal handles: they are used to resolve `business_dependencies` within the
model's own output and then discarded in favour of the keys Jira returns.
"""

from __future__ import annotations

import logging

from agents import (
    AgentOutputError,
    as_int,
    as_str_list,
    detect_cycle,
    invoke_agent_json,
)
from core.prompts import PRODUCT_MANAGER_SYSTEM_PROMPT
from core.state import EpicRef, StoryRef, WorkflowState
from tools import get_jira

logger = logging.getLogger(__name__)

AGENT_NAME = "pm_agent"


def pm_agent_node(state: WorkflowState) -> dict:
    """Run the PM Agent and return a partial state update for LangGraph to merge.

    Emits the structured object defined in the spec's section 7, pushes it into
    Jira, and mirrors the resulting keys back into `epics` / `stories` pointers.
    """
    requirement = state["requirement"]
    if not requirement or not requirement.strip():
        raise AgentOutputError("pm_agent invoked with an empty requirement")

    # A single story for a whole service is almost always under-decomposition,
    # but "add a health endpoint" genuinely is one story -- so this nudges once
    # with the granularity rule rather than refusing outright. Whatever the
    # second attempt returns is accepted: the PM owns that judgment, not this
    # guard, and a hard minimum would mangle a truly atomic requirement.
    nudged = False

    def _check_granularity(candidate: dict) -> None:
        nonlocal nudged
        stories = candidate.get("stories")
        if not isinstance(stories, list) or not stories:
            raise AgentOutputError("output must contain a non-empty 'stories' list")

        # The schema example uses <angle-bracket> slots so it carries no copyable
        # domain content. The cost of that style is echo: a model can return the
        # slot itself. A title containing a bracket is a template, not a story.
        echoed = [
            str(story.get("title", ""))
            for story in stories
            if isinstance(story, dict) and "<" in str(story.get("title", ""))
        ]
        if echoed:
            raise AgentOutputError(
                f"these titles still contain the schema's placeholder brackets: "
                f"{echoed[:3]}. Replace every <...> slot with real content taken "
                f"from the requirement."
            )

        if len(stories) == 1 and not nudged:
            nudged = True
            raise AgentOutputError(
                "you returned a single story for this requirement. Re-read it and "
                "list every independently testable outcome it implies -- creating, "
                "reading, updating, validating, expiring, applying, error cases. "
                "Emit one story per outcome, grouped under one or more epics. "
                "Return a single story ONLY if the requirement is one atomic action."
            )

    result = invoke_agent_json(
        AGENT_NAME,
        PRODUCT_MANAGER_SYSTEM_PROMPT,
        {"requirement": requirement},
        attempts=3,
        validate=_check_granularity,
    )

    raw_epics = result.get("epics")
    raw_stories = result.get("stories")
    if not isinstance(raw_epics, list) or not isinstance(raw_stories, list):
        raise AgentOutputError(
            "pm_agent output must contain 'epics' and 'stories' lists; got keys "
            f"{sorted(result)}"
        )
    if not raw_stories:
        raise AgentOutputError("pm_agent produced no stories")

    jira = get_jira()

    # --- Epics -------------------------------------------------------------
    epics: list[EpicRef] = []
    epic_keys: dict[str, str] = {}  # model's id -> Jira key
    for index, raw in enumerate(raw_epics):
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title", "")).strip() or f"Epic {index + 1}"
        created = jira.create_epic(title)
        epics.append(created)
        epic_keys[str(raw.get("id", "")).strip() or f"__epic_{index}"] = created["id"]

    if not epics:
        raise AgentOutputError("pm_agent produced no epics to hang stories from")
    default_epic_key = epics[0]["id"]

    # --- Stories -----------------------------------------------------------
    # Created first, dependencies linked second: a dependency may point forward
    # to a story that does not exist yet at the moment its holder is created.
    stories: list[StoryRef] = []
    story_keys: dict[str, str] = {}  # model's id -> Jira key
    pending_dependencies: list[tuple[str, list[str]]] = []

    for index, raw in enumerate(raw_stories):
        if not isinstance(raw, dict):
            continue

        model_id = str(raw.get("id", "")).strip() or f"__story_{index}"
        title = str(raw.get("title", "")).strip() or f"Story {index + 1}"
        description = str(raw.get("description", "")).strip()

        criteria = as_str_list(raw.get("acceptance_criteria"))
        if not criteria:
            # Spec section 6: a story with no acceptance criterion is malformed
            # output and must not be emitted. QA later tests against exactly
            # these, so an empty list would leave the story unverifiable.
            raise AgentOutputError(f"pm_agent story {model_id!r} has no acceptance criteria")

        epic_key = epic_keys.get(str(raw.get("epic_id", "")).strip(), default_epic_key)
        priority = as_int(raw.get("priority"), 99)

        created = jira.create_story(
            epic_id=epic_key,
            title=title,
            description=description,
            acceptance_criteria=criteria,
            priority=priority,
        )
        story_keys[model_id] = created["id"]
        stories.append(created)
        pending_dependencies.append((created["id"], as_str_list(raw.get("business_dependencies"))))

        logger.info(
            "[%s] %s (P%s) %s | %d acceptance criteria",
            AGENT_NAME,
            created["id"],
            priority,
            title,
            len(criteria),
        )

    # --- Business dependencies --------------------------------------------
    # Spec section 6: these may only reference stories in this same backlog.
    # Anything unresolvable is dropped rather than handed on as a dangling id
    # that would strand the Developer's READY scan.
    for story, (story_key, raw_dependencies) in zip(stories, pending_dependencies):
        resolved: list[str] = []
        for dependency in raw_dependencies:
            target = story_keys.get(dependency)
            if target is None or target == story_key:
                logger.warning(
                    "[%s] %s: dropped unresolvable business dependency %r",
                    AGENT_NAME,
                    story_key,
                    dependency,
                )
                continue
            resolved.append(target)
            jira.link_dependency(story_key, target)
        story["business_dependencies"] = resolved

    # A cycle here would leave every story in it permanently unready: the
    # Developer's READY scan waits for dependencies to be DONE, and no story in
    # a cycle can ever get there. The run would end as "backlog complete" with
    # nothing built. The edge that closes the cycle is dropped rather than
    # halting -- a circular dependency is meaningless, so the ordering the
    # remaining edges describe is the salvageable part of the model's intent.
    while True:
        cycle = detect_cycle(
            {story["id"]: list(story["business_dependencies"]) for story in stories}
        )
        if not cycle:
            break
        source, target = cycle[-2], cycle[-1]
        for story in stories:
            if story["id"] == source and target in story["business_dependencies"]:
                story["business_dependencies"] = [
                    dep for dep in story["business_dependencies"] if dep != target
                ]
                logger.warning(
                    "[%s] broke a circular business dependency: dropped %s -> %s (cycle %s)",
                    AGENT_NAME,
                    source,
                    target,
                    " -> ".join(cycle),
                )
                break

    logger.info("[%s] backlog: %d epics, %d stories", AGENT_NAME, len(epics), len(stories))
    return {"epics": epics, "stories": stories}


__all__ = ["AGENT_NAME", "pm_agent_node"]
