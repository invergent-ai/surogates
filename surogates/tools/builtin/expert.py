"""Built-in ``consult_expert`` tool -- delegate to an expert model.

The base LLM decides when to consult an expert and receives the
expert's result back as a tool response.  The expert runs its own
scoped mini agent loop with a restricted tool set and bounded
iteration budget.

The handler requires ``tenant``, ``session_id``, ``tool_router``,
and ``tool_registry`` to be passed as keyword arguments by the
harness loop (which injects them automatically via the tool registry
dispatch).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from surogates.tools.builtin.expert_service import ExpertConsultationService
from surogates.tools.loader import SkillDef
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)

_EXPERT_SCHEMA = ToolSchema(
    name="consult_expert",
    description=(
        "Delegate a subtask to a task-specialized expert model. The "
        "expert handles the subtask and returns its result. Use this "
        "when a task falls within an available expert's specialty."
    ),
    parameters={
        "type": "object",
        "properties": {
            "expert": {
                "type": "string",
                "description": "Name of the expert to consult",
            },
            "task": {
                "type": "string",
                "description": "What you want the expert to do",
            },
            "context": {
                "type": "string",
                "description": (
                    "Relevant context the expert needs "
                    "(file contents, error messages, etc.)"
                ),
            },
        },
        "required": ["expert", "task"],
        "additionalProperties": False,
    },
)


def register(registry: ToolRegistry) -> None:
    """Register the ``consult_expert`` tool."""
    registry.register(
        name="consult_expert",
        schema=_EXPERT_SCHEMA,
        handler=_consult_expert_handler,
        toolset="expert",
    )


def get_active_experts(skills: list[SkillDef]) -> list[SkillDef]:
    """Return the subset of *skills* that are active experts.

    Used by :class:`~surogates.harness.prompt.PromptBuilder` to inject
    expert availability into the system prompt.
    """
    return [s for s in skills if s.is_active_expert]


async def _consult_expert_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Resolve the expert, run its mini-loop, and return the result.

    Required kwargs (injected by the harness):
        tenant          -- :class:`~surogates.tenant.context.TenantContext`
        session_id      -- str, the current session UUID
        tool_router     -- :class:`~surogates.tools.router.ToolRouter`
        tool_registry   -- :class:`~surogates.tools.registry.ToolRegistry`
        session_store   -- :class:`~surogates.session.store.SessionStore`
        loaded_skills   -- list[SkillDef], all loaded skills for the tenant
    """
    from uuid import UUID

    tenant = kwargs.get("tenant")
    session_id_str = kwargs.get("session_id")
    tool_router = kwargs.get("tool_router")
    tool_registry = kwargs.get("tool_registry") or kwargs.get("tools")
    session_store = kwargs.get("session_store")
    loaded_skills: list[SkillDef] = kwargs.get("loaded_skills", [])

    if tenant is None:
        return json.dumps({"error": "tenant context not available"})
    if session_id_str is None:
        return json.dumps({"error": "session_id not available"})
    if tool_registry is None:
        return json.dumps({"error": "tool_registry not available"})

    expert_name: str = arguments.get("expert", "")
    task: str = arguments.get("task", "")
    context: str | None = arguments.get("context")

    if not expert_name:
        return json.dumps({"error": "expert name is required"})
    if not task:
        return json.dumps({"error": "task is required"})

    session_id = UUID(str(session_id_str))

    if not loaded_skills:
        try:
            from surogates.tools.builtin.skills import _load_all_skills

            loaded_skills = await _load_all_skills(**kwargs)
        except Exception:
            logger.debug("Failed to load experts for consult_expert", exc_info=True)
            loaded_skills = []

    # Resolve the expert from loaded skills.
    expert = _find_expert(expert_name, loaded_skills)
    if expert is None:
        available = [s.name for s in loaded_skills if s.is_active_expert]
        return json.dumps({
            "error": f"Expert '{expert_name}' not found or not active.",
            "available_experts": available,
        })

    service = ExpertConsultationService(
        tenant=tenant,
        session_id=session_id,
        tool_registry=tool_registry,
        session_store=session_store,
        tool_router=tool_router,
        sandbox_pool=kwargs.get("sandbox_pool"),
    )
    result = await service.consult(
        expert=expert,
        task=task,
        context=context,
    )
    return result.content


def _find_expert(name: str, skills: list[SkillDef]) -> SkillDef | None:
    """Find an active expert by name from the loaded skills list."""
    for skill in skills:
        if skill.name == name and skill.is_active_expert:
            return skill
    return None
