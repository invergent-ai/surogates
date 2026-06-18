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


def _model_id_from_endpoint(endpoint: str | None) -> str | None:
    """Extract the deployed-model id from a ``/proxy/services/_model/{id}`` path.

    Experts served through the platform proxy carry an endpoint like
    ``/proxy/services/_model/<uuid>/v1``; the per-model credential the
    proxy expects is keyed by that ``<uuid>``.  Returns ``None`` for any
    other endpoint shape (self-hosted, dstack legacy) so the caller falls
    back to its default credential.
    """
    if not endpoint:
        return None
    marker = "/_model/"
    idx = endpoint.find(marker)
    if idx == -1:
        return None
    return endpoint[idx + len(marker):].split("/", 1)[0] or None


async def resolve_expert_api_key(
    credential_vault: Any, tenant: Any, expert: SkillDef,
) -> str | None:
    """Resolve the platform credential scoped to the expert's model.

    The expert mini-loop calls ``/proxy/services/_model/{id}``, which the
    proxy gates on an ``sk-agent`` key scoped to that model — the same
    ``vault://byo_model_{id}_key`` credential the platform mints when a
    model is served.  Resolve it from the vault; return ``None`` on any
    miss so the loop falls back to its existing behaviour.
    """
    model_id = _model_id_from_endpoint(expert.expert_endpoint)
    org_id = getattr(tenant, "org_id", None)
    if credential_vault is None or model_id is None or org_id is None:
        return None
    try:
        return await credential_vault.resolve_ref(
            f"vault://byo_model_{model_id}_key", org_id=org_id,
        )
    except Exception:
        logger.debug(
            "expert key resolution failed for model %s", model_id, exc_info=True,
        )
        return None


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
        credential_vault: Any | None = None,
    ) -> None:
        self._tenant = tenant
        self._session_id = session_id
        self._tool_registry = tool_registry
        self._session_store = session_store
        self._tool_router = tool_router
        self._sandbox_pool = sandbox_pool
        self._credential_vault = credential_vault

    async def consult(
        self,
        *,
        expert: SkillDef,
        task: str,
        context: str | None = None,
    ) -> ExpertConsultationResult:
        """Consult *expert* and return a structured result.

        ``expert.delegation`` is emitted first so missing-endpoint and
        run-time failures still produce a joinable row in
        ``v_expert_outcomes`` (the SQL view LEFT JOINs each delegation
        against the nearest outcome event).
        """
        await self._emit_delegation(expert=expert, task=task)

        if not expert.expert_endpoint:
            error = f"Expert '{expert.name}' has no endpoint configured."
            await self._record_failure(expert, error)
            return ExpertConsultationResult(
                expert=expert.name,
                success=False,
                content=json.dumps({"error": error}),
                error=error,
            )

        try:
            api_key = await resolve_expert_api_key(
                self._credential_vault, self._tenant, expert,
            )
            result, iterations_used = await run_expert_loop(
                expert=expert,
                task=task,
                context=context,
                tool_router=self._resolve_tool_router(),
                tool_registry=self._tool_registry,
                tenant=self._tenant,
                session_id=self._session_id,
                session_store=self._session_store,
                api_key=api_key,
            )
            await record_expert_outcome(
                session_store=self._session_store,
                session_id=self._session_id,
                expert_name=expert.name,
                success=True,
                iterations_used=iterations_used,
                content=result,
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
            await self._record_failure(expert, str(exc))
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
    ) -> None:
        if self._session_store is None:
            return
        data: dict[str, Any] = {
            "expert": expert.name,
            "task": task[:500],
            "tools": expert.expert_tools or [],
            "max_iterations": expert.expert_max_iterations,
        }
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
        iterations_used: int = 0,
    ) -> None:
        await record_expert_outcome(
            session_store=self._session_store,
            session_id=self._session_id,
            expert_name=expert.name,
            success=False,
            iterations_used=iterations_used,
            error=error,
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
