"""Memory, skill and agent routes refuse channel principals."""

from __future__ import annotations

import inspect
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException

from surogates.tenant.context import TenantContext


def _channel_ctx(tmp_path: Path) -> TenantContext:
    """A channel-session context (only ``session_scope_id`` set)."""
    return TenantContext(
        org_id=uuid4(),
        user_id=None,
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root=str(tmp_path),
        service_account_id=None,
        session_scope_id=uuid4(),
    )


@pytest.mark.asyncio
class TestMemoryRouteGate:
    """Channel principals must not read or write memory.

    Memory routes pass ``tenant.user_id`` straight to ``TenantStorage``,
    which maps ``user_id=None`` to ``shared/memory/*``.  The gate is the
    hard boundary that keeps channel JWTs out of shared memory while
    leaving service-account contexts (also ``user_id=None``) intact.
    """

    async def test_get_memory_refuses_channel_principal(
        self, tmp_path: Path,
    ):
        from surogates.api.routes import memory as memory_routes

        with pytest.raises(HTTPException) as exc:
            await memory_routes.get_memory(
                request=None,  # gate fires before request is read
                tenant=_channel_ctx(tmp_path),
            )
        assert exc.value.status_code == 403

    async def test_mutate_memory_refuses_channel_principal(
        self, tmp_path: Path,
    ):
        from surogates.api.routes import memory as memory_routes

        with pytest.raises(HTTPException) as exc:
            await memory_routes.mutate_memory(
                body=None,
                request=None,
                tenant=_channel_ctx(tmp_path),
            )
        assert exc.value.status_code == 403


_MUTATING_SKILL_HANDLERS = (
    "create_skill",
    "edit_skill",
    "patch_skill",
    "delete_skill",
    "write_skill_file",
    "remove_skill_file",
)


def _kwargs_for_handler(handler, tenant: TenantContext) -> dict:
    """Build kwargs satisfying *handler*'s signature.

    Required parameters that aren't ``tenant`` get ``None``; the gate
    must fire before any of them is dereferenced.  Parameters with
    defaults are omitted so FastAPI's ``Depends(...)`` defaults remain
    in place (and the gate uses the ``tenant`` kwarg we pass).
    """
    sig = inspect.signature(handler)
    kwargs: dict = {}
    for name, param in sig.parameters.items():
        if name == "tenant":
            kwargs[name] = tenant
        elif param.default is not inspect.Parameter.empty:
            continue
        else:
            kwargs[name] = None
    return kwargs


@pytest.mark.asyncio
class TestMutatingSkillsRouteGate:
    """Every mutate handler must refuse channel principals."""

    @pytest.mark.parametrize("handler_name", _MUTATING_SKILL_HANDLERS)
    async def test_refuses_channel_principal(
        self, tmp_path: Path, handler_name: str,
    ):
        from surogates.api.routes import skills as skills_routes

        handler = getattr(skills_routes, handler_name)
        kwargs = _kwargs_for_handler(handler, _channel_ctx(tmp_path))

        with pytest.raises(HTTPException) as exc:
            await handler(**kwargs)
        assert exc.value.status_code == 403, (
            f"{handler_name} must refuse channel principals"
        )


_AGENT_HANDLERS = (
    "list_agents",
    "view_agent",
    "create_agent",
    "edit_agent",
    "delete_agent",
)


@pytest.mark.asyncio
class TestAgentsRouteGate:
    """Every ``/v1/agents`` handler refuses channel principals.

    Sub-agent definitions are deployment-private metadata; anonymous
    visitors must not enumerate or modify them even through a leaked
    channel JWT.
    """

    @pytest.mark.parametrize("handler_name", _AGENT_HANDLERS)
    async def test_refuses_channel_principal(
        self, tmp_path: Path, handler_name: str,
    ):
        from surogates.api.routes import agents as agents_routes

        handler = getattr(agents_routes, handler_name)
        kwargs = _kwargs_for_handler(handler, _channel_ctx(tmp_path))

        with pytest.raises(HTTPException) as exc:
            await handler(**kwargs)
        assert exc.value.status_code == 403, (
            f"{handler_name} must refuse channel principals"
        )
