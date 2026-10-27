"""Memory, skill and agent routes refuse channel principals."""

from __future__ import annotations

import inspect
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException

from surogates.tenant.context import TenantContext
from tests.test_memory_route_session_boundary import _fixtures, _mutate, _session


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


def _channel_tenant(session) -> TenantContext:
    """The context a worker-minted ``channel_session`` token gives *session*."""
    return TenantContext(
        org_id=session.org_id,
        user_id=None,
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root="/tmp/assets",
        service_account_id=None,
        session_scope_id=session.id,
    )


@pytest.mark.asyncio
class TestMemoryRouteGate:
    """Channel principals reach only a website visitor's own memory.

    Memory routes key a principal with no user and no boundary to
    ``shared/*``, the agent's org-wide memory.  A channel token is refused
    unless its session carries a ``visitor:`` partition, so it can never
    read or write that shared store, while a website visitor's memory tool
    reads and writes the visitor's own partition.
    """

    async def test_get_memory_refuses_channel_principal_without_visitor_partition(self):
        from surogates.api.routes import memory as memory_routes

        org_id = uuid4()
        session = _session(org_id, {})
        _backend, request, _tenant, agent_runtime = _fixtures(session, org_id=org_id)

        with pytest.raises(HTTPException) as exc:
            await memory_routes.get_memory(
                request, None, _channel_tenant(session), agent_runtime,
            )
        assert exc.value.status_code == 403

    async def test_mutate_memory_refuses_channel_principal_without_visitor_partition(self):
        org_id = uuid4()
        # A visitor boundary on a session of any other channel is not honoured.
        session = _session(org_id, {"memory_boundary": "visitor:" + "0" * 32})
        backend, request, _tenant, agent_runtime = _fixtures(session, org_id=org_id)

        with pytest.raises(HTTPException) as exc:
            await _mutate(request, _channel_tenant(session), agent_runtime, None)
        assert exc.value.status_code == 403
        assert backend.written == []

    async def test_a_website_visitor_writes_only_its_own_partition(self):
        org_id = uuid4()
        boundary = "visitor:" + "a" * 32
        session = _session(org_id, {"memory_boundary": boundary})
        session.channel = "website"
        backend, request, _tenant, agent_runtime = _fixtures(session, org_id=org_id)

        result = await _mutate(request, _channel_tenant(session), agent_runtime, None)

        assert result.success is True
        assert backend.written == [f"agents/support-bot/boundaries/{boundary}/memory.json"]

    async def test_a_channel_token_cannot_name_another_session(self):
        org_id = uuid4()
        session = _session(org_id, {"memory_boundary": "visitor:" + "a" * 32})
        session.channel = "website"
        _backend, request, _tenant, agent_runtime = _fixtures(session, org_id=org_id)
        other = _session(org_id, {})

        with pytest.raises(HTTPException) as exc:
            await _mutate(request, _channel_tenant(other), agent_runtime, session.id)
        assert exc.value.status_code == 404


_MUTATING_SKILL_HANDLERS = (
    "create_skill",
    "edit_skill",
    "patch_skill",
    "delete_skill",
    "write_skill_file",
    "remove_skill_file",
    "activate_expert",
    "retire_expert",
    "collect_training_data",
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
