"""Tests for the agent/user SSH-key vault routes (surogates api)."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from surogates.api.routes import ssh_credentials
from surogates.runtime import agent_runtime_context_dep
from surogates.ssh_access.bundle import SSHKeyBundle
from surogates.tenant.auth.middleware import get_current_tenant

pytestmark = pytest.mark.asyncio

USER = UUID("11111111-1111-1111-1111-111111111111")
ORG = UUID("22222222-2222-2222-2222-222222222222")

_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n"


class _FakeVault:
    def __init__(self):
        self.stored: dict[tuple, str] = {}

    async def store(self, org_id, name, value, *, user_id=None, service_account_id=None):
        self.stored[(org_id, name)] = value

    async def list_names(self, org_id, user_id=None, *, service_account_id=None):
        return [name for (o, name) in self.stored if o == org_id]

    async def delete(self, org_id, name, *, user_id=None, service_account_id=None):
        self.stored.pop((org_id, name), None)


def _make_app(*, tenant, ctx, vault):
    app = FastAPI()
    app.state.credential_vault = vault
    app.dependency_overrides[get_current_tenant] = lambda: tenant
    app.dependency_overrides[agent_runtime_context_dep] = lambda: ctx
    app.include_router(ssh_credentials.router, prefix="/v1/api")
    return app


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _tenant(user_id=USER, org_id=ORG, service_account_id=None):
    return SimpleNamespace(
        user_id=user_id, org_id=org_id, service_account_id=service_account_id,
    )


def _ctx(org_id=str(ORG)):
    return SimpleNamespace(agent_id="agent-1", org_id=org_id)


async def test_store_and_list_ssh_key():
    vault = _FakeVault()
    app = _make_app(tenant=_tenant(), ctx=_ctx(), vault=vault)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/api/ssh-credentials/credential",
            json={"name": "prod", "private_key": _KEY, "passphrase": "pw"},
        )
        assert resp.status_code == 200, resp.text
        listing = await client.get("/v1/api/ssh-credentials/credentials")
    stored = vault.stored[(ORG, "ssh_key:prod")]
    bundle = SSHKeyBundle.from_json(stored)
    assert bundle.private_key == _KEY and bundle.passphrase == "pw"
    assert "prod" in [c["name"] for c in listing.json()["credentials"]]


async def test_reject_non_key():
    app = _make_app(tenant=_tenant(), ctx=_ctx(), vault=_FakeVault())
    async with _client(app) as client:
        resp = await client.post(
            "/v1/api/ssh-credentials/credential",
            json={"name": "prod", "private_key": "nope"},
        )
    assert resp.status_code == 422


async def test_reject_bad_name():
    app = _make_app(tenant=_tenant(), ctx=_ctx(), vault=_FakeVault())
    async with _client(app) as client:
        resp = await client.post(
            "/v1/api/ssh-credentials/credential",
            json={"name": "a:b", "private_key": _KEY},
        )
    assert resp.status_code == 422


async def test_delete_ssh_key():
    vault = _FakeVault()
    vault.stored[(ORG, "ssh_key:prod")] = SSHKeyBundle(private_key=_KEY).to_json()
    app = _make_app(tenant=_tenant(), ctx=_ctx(), vault=vault)
    async with _client(app) as client:
        resp = await client.delete(
            "/v1/api/ssh-credentials/credential", params={"name": "prod"},
        )
    assert resp.status_code == 204
    assert (ORG, "ssh_key:prod") not in vault.stored


async def test_requires_identity():
    app = _make_app(
        tenant=_tenant(user_id=None, service_account_id=None), ctx=_ctx(), vault=_FakeVault(),
    )
    async with _client(app) as client:
        resp = await client.get("/v1/api/ssh-credentials/credentials")
    assert resp.status_code == 401
