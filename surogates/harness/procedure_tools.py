"""Which sessions get the procedure step-marker tool.

``skill_step`` records where the model is in a graph-backed procedure
skill. It is offered exactly when such a skill is in the session's
catalog: an agent without one has no use for the schema, and an agent
with one needs the tool even when an explicit allow list predates the
skill, since attaching the procedure is the explicit intent.
"""
from __future__ import annotations

from typing import Any, Iterable

SKILL_STEP_TOOL = "skill_step"


def gate_skill_step(
    tool_names: set[str] | None,
    *,
    all_tools: Iterable[str],
    skills: Iterable[Any],
) -> set[str] | None:
    """*tool_names* with ``skill_step`` added or removed by the rule above.

    ``None`` means every registered tool; it stays ``None`` when the tool
    should be present and is materialised without it otherwise.
    """
    registered = set(all_tools)
    wanted = SKILL_STEP_TOOL in registered and any(
        getattr(skill, "has_graph", False) for skill in skills
    )
    if tool_names is None:
        if wanted:
            return None
        return registered - {SKILL_STEP_TOOL}
    updated = set(tool_names)
    if wanted:
        updated.add(SKILL_STEP_TOOL)
    else:
        updated.discard(SKILL_STEP_TOOL)
    return updated
