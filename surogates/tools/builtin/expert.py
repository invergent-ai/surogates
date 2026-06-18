"""Built-in ``consult_expert`` tool -- voluntary consultation of a specialist model.

The base LLM decides when to consult an expert and receives the
expert's deliverable back as a tool response.  The expert runs its
own scoped mini agent loop with a restricted tool set and bounded
iteration budget.  This is distinct from ``delegate_task``, which
spawns sub-agents in fresh sessions for multi-step work.

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
from surogates.tools.loader import (
    EXPERT_STATUS_ACTIVE,
    EXPERT_STATUS_DRAFT,
    SkillDef,
)
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)

_EXPERT_SCHEMA = ToolSchema(
    name="consult_expert",
    description=(
        "Consult a specialist model for a single domain question. The "
        "expert answers and returns its deliverable. Use this when a "
        "request falls within an available expert's specialty."
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

    # Resolve the expert.  In the shared-runtime (API-mediated) deployment
    # per-agent bundle experts are only visible to the bundle-aware API
    # server, so we must resolve through ``api_client`` -- the same path
    # ``skills_list``/``skill_view`` already take.  The worker-local loader
    # is bundle-blind and would report the expert as "not found".
    api_client = kwargs.get("api_client")
    if api_client is not None:
        expert, available = await _resolve_expert_via_api(api_client, expert_name)
    else:
        expert, available = await _resolve_expert_local(
            expert_name, loaded_skills, kwargs,
        )

    if expert is None:
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
        credential_vault=kwargs.get("credential_vault"),
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


async def _resolve_expert_local(
    expert_name: str,
    loaded_skills: list[SkillDef],
    kwargs: dict[str, Any],
) -> tuple[SkillDef | None, list[str]]:
    """Resolve an expert from the worker-local skill layers (embedded mode).

    Returns ``(expert_or_None, available_active_expert_names)``.
    """
    if not loaded_skills:
        try:
            from surogates.tools.builtin.skills import _load_all_skills

            loaded_skills = await _load_all_skills(**kwargs)
        except Exception:
            logger.debug("Failed to load experts for consult_expert", exc_info=True)
            loaded_skills = []
    expert = _find_expert(expert_name, loaded_skills)
    available = [s.name for s in loaded_skills if s.is_active_expert]
    return expert, available


async def _resolve_expert_via_api(
    api_client: Any,
    expert_name: str,
) -> tuple[SkillDef | None, list[str]]:
    """Resolve an expert through the bundle-aware API server.

    Returns ``(expert_or_None, available_active_expert_names)``.  The
    detail endpoint sees the per-agent bundle, so experts attached to the
    agent (``source: platform``) resolve here even though they never reach
    the worker's local loader.
    """
    detail: dict[str, Any] | None = None
    try:
        detail = await api_client.get_skill(expert_name)
    except Exception:
        logger.debug(
            "api_client.get_skill failed for expert=%s", expert_name, exc_info=True,
        )
    if detail:
        candidate = _skill_def_from_detail(detail)
        if candidate is not None and candidate.is_active_expert:
            return candidate, []
    return None, await _active_experts_via_api(api_client)


async def _active_experts_via_api(api_client: Any) -> list[str]:
    """Return the names of active experts from the bundle-aware catalog."""
    try:
        catalog = json.loads(await api_client.list_skills())
    except Exception:
        logger.debug("api_client.list_skills failed", exc_info=True)
        return []
    return [
        s.get("name")
        for s in catalog.get("skills", [])
        if s.get("type") == "expert"
        and s.get("expert_status") == EXPERT_STATUS_ACTIVE
        and s.get("name")
    ]


def _skill_def_from_detail(detail: dict[str, Any]) -> SkillDef | None:
    """Reconstruct a :class:`SkillDef` from a ``/v1/skills/{name}`` payload."""
    name = detail.get("name")
    if not name:
        return None
    max_iter = detail.get("expert_max_iterations")
    return SkillDef(
        name=name,
        description=detail.get("description") or "",
        content=detail.get("content") or "",
        source=detail.get("source") or "platform",
        type=detail.get("type") or "skill",
        category=detail.get("category"),
        trigger=detail.get("trigger"),
        expert_model=detail.get("expert_model"),
        expert_endpoint=detail.get("expert_endpoint"),
        expert_adapter=detail.get("expert_adapter"),
        expert_tools=detail.get("expert_tools"),
        expert_max_iterations=int(max_iter) if max_iter is not None else 10,
        expert_status=detail.get("expert_status") or EXPERT_STATUS_DRAFT,
    )
