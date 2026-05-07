"""Shared expert consultation service for tool and harness routing paths."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from surogates.session.events import EventType
from surogates.tools.builtin.expert_feedback import record_expert_outcome
from surogates.tools.builtin.expert_loop import ExpertBudgetExceeded, run_expert_loop
from surogates.tools.loader import SkillDef

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExpertConsultationResult:
    """Result returned from an expert consultation."""

    expert: str
    success: bool
    content: str
    iterations_used: int = 0
    error: str | None = None


class ExpertConsultationService:
    """Run expert consultations with common events, stats, and fallbacks."""

    def __init__(
        self,
        *,
        tenant: Any,
        session_id: UUID,
        tool_registry: Any,
        session_store: Any | None = None,
        tool_router: Any | None = None,
        sandbox_pool: Any | None = None,
    ) -> None:
        self._tenant = tenant
        self._session_id = session_id
        self._tool_registry = tool_registry
        self._session_store = session_store
        self._tool_router = tool_router
        self._sandbox_pool = sandbox_pool

    async def consult(
        self,
        *,
        expert: SkillDef,
        task: str,
        context: str | None = None,
        forced: bool = False,
        category: str | None = None,
    ) -> ExpertConsultationResult:
        """Consult *expert* and return a structured result."""
        if not expert.expert_endpoint:
            error = f"Expert '{expert.name}' has no endpoint configured."
            await self._record_failure(
                expert, error, forced=forced, category=category,
            )
            return ExpertConsultationResult(
                expert=expert.name,
                success=False,
                content=json.dumps({"error": error}),
                error=error,
            )

        await self._emit_delegation(
            expert=expert,
            task=task,
            forced=forced,
            category=category,
        )

        try:
            result, iterations_used = await run_expert_loop(
                expert=expert,
                task=task,
                context=context,
                tool_router=self._resolve_tool_router(),
                tool_registry=self._tool_registry,
                tenant=self._tenant,
                session_id=self._session_id,
                session_store=self._session_store,
            )
            await record_expert_outcome(
                session_store=self._session_store,
                session_id=self._session_id,
                expert_name=expert.name,
                success=True,
                iterations_used=iterations_used,
                content=result,
                forced=forced,
                category=category,
            )
            return ExpertConsultationResult(
                expert=expert.name,
                success=True,
                content=result,
                iterations_used=iterations_used,
            )
        except ExpertBudgetExceeded as exc:
            logger.warning("Expert %s budget exceeded: %s", expert.name, exc)
            await self._record_failure(
                expert,
                str(exc),
                forced=forced,
                category=category,
                iterations_used=expert.expert_max_iterations,
            )
            return ExpertConsultationResult(
                expert=expert.name,
                success=False,
                content=json.dumps({
                    "error": str(exc),
                    "suggestion": "The expert could not complete the task "
                    "within its iteration budget. The default model may proceed.",
                }),
                iterations_used=expert.expert_max_iterations,
                error=str(exc),
            )
        except Exception as exc:
            logger.exception("Expert %s failed with error", expert.name)
            await self._record_failure(
                expert, str(exc), forced=forced, category=category,
            )
            return ExpertConsultationResult(
                expert=expert.name,
                success=False,
                content=json.dumps({
                    "error": f"Expert '{expert.name}' failed: {exc}",
                }),
                error=str(exc),
            )

    async def _emit_delegation(
        self,
        *,
        expert: SkillDef,
        task: str,
        forced: bool,
        category: str | None,
    ) -> None:
        if self._session_store is None:
            return
        data: dict[str, Any] = {
            "expert": expert.name,
            "task": task[:500],
            "tools": expert.expert_tools or [],
            "max_iterations": expert.expert_max_iterations,
        }
        if forced:
            data["forced"] = True
        if category:
            data["category"] = category
        try:
            await self._session_store.emit_event(
                self._session_id,
                EventType.EXPERT_DELEGATION,
                data,
            )
        except Exception:
            logger.debug("Failed to emit EXPERT_DELEGATION event", exc_info=True)

    async def _record_failure(
        self,
        expert: SkillDef,
        error: str,
        *,
        forced: bool,
        category: str | None,
        iterations_used: int = 0,
    ) -> None:
        await record_expert_outcome(
            session_store=self._session_store,
            session_id=self._session_id,
            expert_name=expert.name,
            success=False,
            iterations_used=iterations_used,
            error=error,
            forced=forced,
            category=category,
        )

    def _resolve_tool_router(self) -> Any:
        if self._tool_router is not None:
            return self._tool_router
        if self._sandbox_pool is None:
            return _RegistryOnlyToolRouter(self._tool_registry)

        from surogates.governance.policy import GovernanceGate
        from surogates.tools.router import ToolRouter

        return ToolRouter(
            self._tool_registry,
            self._sandbox_pool,
            GovernanceGate(),
        )


class _RegistryOnlyToolRouter:
    """Minimal fallback router for tests and harness-local expert tools."""

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    async def execute(
        self,
        *,
        name: str,
        arguments: str | dict[str, Any],
        tenant: Any,
        session_id: UUID,
        workspace_path: str | None = None,
    ) -> str:
        return await self._registry.dispatch(
            name,
            arguments,
            tenant=tenant,
            session_id=session_id,
        )
