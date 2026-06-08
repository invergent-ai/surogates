"""Tests for the end-user Composio connect routes (surogates api)."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from surogates.api.routes import composio
from surogates.runtime import agent_runtime_context_dep
from surogates.tenant.auth.middleware import get_current_tenant

pytestmark = pytest.mark.asyncio

USER = UUID("11111111-1111-1111-1111-111111111111")
ORG = UUID("22222222-2222-2222-2222-222222222222")


class _FakePC:
    def __init__(self):
        self.calls = []

    async def composio_connections(self, agent_id, user_id):
        self.calls.append(("connections", agent_id, user_id))
        return {"toolkits": [{"toolkit": "github", "connected": True}]}

    async def composio_authorize(self, agent_id, user_id, toolkit):
        self.calls.append(("authorize", agent_id, user_id, toolkit))
        return {"redirect_url": "https://p/oauth", "connection_request_id": "cr_1", "status": "INITIATED"}

    async def composio_disconnect(self, agent_id, user_id, toolkit):
        self.calls.append(("disconnect", agent_id, user_id, toolkit))
        return {"disconnected": 1}


def _make_app(*, tenant, ctx, pc):
    app = FastAPI()
    app.state.platform_client = pc
    app.dependency_overrides[get_current_tenant] = lambda: tenant
    app.dependency_overrides[agent_runtime_context_dep] = lambda: ctx
    app.include_router(composio.router, prefix="/v1")
    return app


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _tenant(user_id=USER, org_id=ORG):
    return SimpleNamespace(user_id=user_id, org_id=org_id, service_account_id=None)


def _ctx(agent_id="agent-1", org_id=str(ORG)):
    return SimpleNamespace(agent_id=agent_id, org_id=org_id)


async def test_connections_forwards_tenant_user_and_runtime_agent():
    pc = _FakePC()
    app = _make_app(tenant=_tenant(), ctx=_ctx(), pc=pc)
    async with _client(app) as client:
        r = await client.get("/v1/composio/connections")
    assert r.status_code == 200, r.text
    assert r.json() == {"toolkits": [{"toolkit": "github", "connected": True}]}
    assert pc.calls[0] == ("connections", "agent-1", str(USER))


async def test_authorize_forwards_toolkit():
    pc = _FakePC()
    app = _make_app(tenant=_tenant(), ctx=_ctx(), pc=pc)
    async with _client(app) as client:
        r = await client.post("/v1/composio/toolkits/github/authorize")
    assert r.status_code == 200, r.text
    assert r.json()["redirect_url"] == "https://p/oauth"
    assert pc.calls[0] == ("authorize", "agent-1", str(USER), "github")


async def test_disconnect_route_forwards_to_platform_client():
    pc = _FakePC()
    app = _make_app(tenant=_tenant(), ctx=_ctx(), pc=pc)
    async with _client(app) as client:
        r = await client.request("DELETE", "/v1/composio/toolkits/gmail/connection")
    assert r.status_code == 200, r.text
    assert r.json() == {"disconnected": 1}
    assert pc.calls[0] == ("disconnect", "agent-1", str(USER), "gmail")


async def test_rejects_when_no_tenant_user():
    app = _make_app(tenant=_tenant(user_id=None), ctx=_ctx(), pc=_FakePC())
    async with _client(app) as client:
        r = await client.get("/v1/composio/connections")
    assert r.status_code == 401, r.text


async def test_rejects_cross_org():
    # tenant.org_id != ctx.org_id
    app = _make_app(tenant=_tenant(), ctx=_ctx(org_id="33333333-3333-3333-3333-333333333333"), pc=_FakePC())
    async with _client(app) as client:
        r = await client.get("/v1/composio/connections")
    assert r.status_code == 403, r.text
