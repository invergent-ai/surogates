"""Integration tests for the /v1/coding-agents routes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from surogates.api.routes import coding_agents
from surogates.runtime import agent_runtime_context_dep
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext
from surogates.tenant.credentials import CredentialVault

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def client(session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    app = FastAPI()
    app.include_router(coding_agents.router, prefix="/v1")
    app.state.credential_vault = CredentialVault(session_factory, Fernet.generate_key())

    tenant = TenantContext(
        org_id=org_id, user_id=user_id, org_config={}, user_preferences={},
        permissions=frozenset({"read", "write"}), asset_root="/tmp",
    )
    app.dependency_overrides[get_current_tenant] = lambda: tenant
    app.dependency_overrides[agent_runtime_context_dep] = lambda: SimpleNamespace(
        org_id=str(org_id), agent_id="agent-test",
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        yield c


async def test_connections_starts_empty(client):
    resp = await client.get("/v1/coding-agents/connections")
    assert resp.status_code == 200
    by_provider = {c["provider"]: c for c in resp.json()["connections"]}
    assert by_provider["anthropic"]["connected"] is False
    assert by_provider["openai"]["connected"] is False


async def test_submit_then_connected(client):
    resp = await client.post(
        "/v1/coding-agents/anthropic/credential",
        json={"mode": "oauth", "value": "sk-ant-oat01-abc"},
    )
    assert resp.status_code == 200
    assert resp.json()["connected"] is True

    listed = await client.get("/v1/coding-agents/connections")
    by_provider = {c["provider"]: c for c in listed.json()["connections"]}
    assert by_provider["anthropic"]["connected"] is True


async def test_submit_invalid_returns_422(client):
    resp = await client.post(
        "/v1/coding-agents/anthropic/credential",
        json={"mode": "oauth", "value": "not-a-token"},
    )
    assert resp.status_code == 422


async def test_delete(client):
    await client.post(
        "/v1/coding-agents/openai/credential",
        json={"mode": "api_key", "value": "sk-proj-abc"},
    )
    resp = await client.delete("/v1/coding-agents/openai")
    assert resp.status_code == 204

    resp2 = await client.delete("/v1/coding-agents/openai")
    assert resp2.status_code == 404
