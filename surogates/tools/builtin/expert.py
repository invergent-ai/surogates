"""Built-in ``consult_expert`` tool -- delegate a subtask to a fine-tuned SLM.

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

from surogates.session.events import EventType
from surogates.tools.builtin.expert_feedback import record_expert_outcome
from surogates.tools.builtin.expert_loop import ExpertBudgetExceeded, run_expert_loop
from surogates.tools.loader import SkillDef
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)

_EXPERT_SCHEMA = ToolSchema(
    name="consult_expert",
    description=(
        "Delegate a subtask to a specialised expert model. Experts are "
        "fine-tuned on this organisation's patterns and data. The expert "
        "handles the subtask and returns its result. Use this when a "
        "task falls within an available expert's specialty."
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
    tool_registry = kwargs.get("tool_registry")
    session_store = kwargs.get("session_store")
    loaded_skills: list[SkillDef] = kwargs.get("loaded_skills", [])

    if tenant is None:
        return json.dumps({"error": "tenant context not available"})
    if session_id_str is None:
        return json.dumps({"error": "session_id not available"})
    if tool_router is None:
        return json.dumps({"error": "tool_router not available"})
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

    # Resolve the expert from loaded skills.
    expert = _find_expert(expert_name, loaded_skills)
    if expert is None:
        available = [s.name for s in loaded_skills if s.is_active_expert]
        return json.dumps({
            "error": f"Expert '{expert_name}' not found or not active.",
            "available_experts": available,
        })

    # Validate the expert has an endpoint configured.
    if not expert.expert_endpoint:
        return json.dumps({
            "error": f"Expert '{expert_name}' has no endpoint configured.",
        })

    # Emit delegation event.
    if session_store is not None:
        try:
            await session_store.emit_event(
                session_id,
                EventType.EXPERT_DELEGATION,
                {
                    "expert": expert_name,
                    "task": task[:500],
                    "tools": expert.expert_tools or [],
                    "max_iterations": expert.expert_max_iterations,
                },
            )
        except Exception:
            logger.debug("Failed to emit EXPERT_DELEGATION event", exc_info=True)

    # Run the expert's mini agent loop.
    try:
        result, iterations_used = await run_expert_loop(
            expert=expert,
            task=task,
            context=context,
            tool_router=tool_router,
            tool_registry=tool_registry,
            tenant=tenant,
            session_id=session_id,
            session_store=session_store,
        )

        await record_expert_outcome(
            session_store=session_store,
            session_id=session_id,
            expert_name=expert_name,
            success=True,
            iterations_used=iterations_used,
        )

        return result

    except ExpertBudgetExceeded as exc:
        logger.warning("Expert %s budget exceeded: %s", expert_name, exc)
        await record_expert_outcome(
            session_store=session_store,
            session_id=session_id,
            expert_name=expert_name,
            success=False,
            iterations_used=expert.expert_max_iterations,
            error=str(exc),
        )
        return json.dumps({
            "error": str(exc),
            "suggestion": "The expert could not complete the task within its "
            "iteration budget. You may want to handle this task directly.",
        })

    except Exception as exc:
        logger.exception("Expert %s failed with error", expert_name)
        await record_expert_outcome(
            session_store=session_store,
            session_id=session_id,
            expert_name=expert_name,
            success=False,
            error=str(exc),
        )
        return json.dumps({
            "error": f"Expert '{expert_name}' failed: {exc}",
        })


def _find_expert(name: str, skills: list[SkillDef]) -> SkillDef | None:
    """Find an active expert by name from the loaded skills list."""
    for skill in skills:
        if skill.name == name and skill.is_active_expert:
            return skill
    return None
