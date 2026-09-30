"""Desktop devices: registration, presence and the device link."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from uuid import UUID

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from surogates.db.agent_users import purge_user_account
from surogates.session.store import SessionStore
from surogates.tenant.auth.jwt import create_access_token
from surogates.tenant.credentials import CredentialVault

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

AGENT_ID = "agent-devices"


@dataclass
class Api:
    client: AsyncClient
    app: FastAPI
    org_id: UUID
    user_id: UUID
    token: str

    def auth(self, token: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token or self.token}"}


async def add_user(session_factory, org_id: UUID) -> tuple[UUID, str]:
    user_id = uuid.uuid4()
    await create_user(
        session_factory, org_id, user_id=user_id,
        email=f"user-{user_id}@test.com", password="testpass123",
    )
    return user_id, create_access_token(org_id, user_id, {"sessions:read", "sessions:write"})


@pytest_asyncio.fixture(loop_scope="session")
async def api(session_factory, redis_client, pg_url, redis_url):
    """The real app, wired to the test containers, serving one agent of a fresh org."""
    os.environ["SUROGATES_DB_URL"] = pg_url
    os.environ["SUROGATES_REDIS_URL"] = redis_url

    from surogates.api.app import create_app
    from surogates.config import Settings
    from surogates.runtime import agent_runtime_context_dep, build_agent_runtime_context
    from surogates.storage.backend import create_backend

    org_id = await create_org(session_factory)
    user_id, token = await add_user(session_factory, org_id)

    app = create_app()
    app.state.session_factory = session_factory
    app.state.redis = redis_client
    app.state.session_store = SessionStore(session_factory)
    app.state.settings = Settings()
    app.state.settings.storage.bucket = f"test-agent-{uuid.uuid4()}"
    app.state.storage = create_backend(app.state.settings)
    app.state.credential_vault = CredentialVault(session_factory, Fernet.generate_key())
    app.dependency_overrides[agent_runtime_context_dep] = lambda: build_agent_runtime_context({
        "agent_id": AGENT_ID,
        "org_id": str(org_id),
        "project_id": "test-project",
        "enabled": True,
        "version": 1,
        "storage_key_prefix": "",
    })
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield Api(client, app, org_id, user_id, token)


async def register(api: Api, name: str = "Flavius's ThinkPad", token: str | None = None) -> dict:
    response = await api.client.post("/v1/devices", json={"name": name}, headers=api.auth(token))
    assert response.status_code == 201, response.text
    return response.json()


async def test_registration_returns_the_token_once(api):
    issued = await register(api)
    assert issued["token"].startswith("surg_dev_")
    assert issued["token_prefix"] == issued["token"][:17]
    assert issued["name"] == "Flavius's ThinkPad"
    assert issued["revoked_at"] is None

    listed = (await api.client.get("/v1/devices", headers=api.auth())).json()
    assert [d["id"] for d in listed] == [issued["id"]]
    assert "token" not in listed[0]


@pytest.mark.parametrize("name", ["   ", "x" * 101])
async def test_a_blank_or_long_name_is_rejected(api, name):
    response = await api.client.post("/v1/devices", json={"name": name}, headers=api.auth())
    assert response.status_code == 422


async def test_a_user_sees_and_revokes_only_their_own_devices(api, session_factory):
    other_id, other_token = await add_user(session_factory, api.org_id)
    mine = await register(api)
    theirs = await register(api, name="Other laptop", token=other_token)

    listed = (await api.client.get("/v1/devices", headers=api.auth())).json()
    assert [d["id"] for d in listed] == [mine["id"]]

    response = await api.client.delete(f"/v1/devices/{theirs['id']}", headers=api.auth())
    assert response.status_code == 404
    response = await api.client.post(
        f"/v1/devices/{theirs['id']}/reauthorize", headers=api.auth(),
    )
    assert response.status_code == 404


async def test_revoking_is_idempotent(api):
    issued = await register(api)
    first = await api.client.delete(f"/v1/devices/{issued['id']}", headers=api.auth())
    assert first.status_code == 204
    revoked_at = (await api.client.get("/v1/devices", headers=api.auth())).json()[0]["revoked_at"]
    assert revoked_at is not None

    second = await api.client.delete(f"/v1/devices/{issued['id']}", headers=api.auth())
    assert second.status_code == 204
    again = (await api.client.get("/v1/devices", headers=api.auth())).json()[0]["revoked_at"]
    assert again == revoked_at


async def test_reauthorizing_restores_a_revoked_device_with_a_new_token(api):
    issued = await register(api)
    await api.client.delete(f"/v1/devices/{issued['id']}", headers=api.auth())

    response = await api.client.post(
        f"/v1/devices/{issued['id']}/reauthorize", headers=api.auth(),
    )
    assert response.status_code == 200, response.text
    restored = response.json()
    assert restored["id"] == issued["id"]
    assert restored["token"].startswith("surg_dev_")
    assert restored["token"] != issued["token"]
    assert restored["token_prefix"] == restored["token"][:17]
    assert restored["revoked_at"] is None


async def test_a_user_of_another_org_cannot_register(api, session_factory):
    other_org = await create_org(session_factory)
    _, stranger = await add_user(session_factory, other_org)
    response = await api.client.post("/v1/devices", json={"name": "x"}, headers=api.auth(stranger))
    assert response.status_code == 403


async def test_a_device_token_is_not_a_user_credential(api):
    issued = await register(api)
    response = await api.client.get("/v1/devices", headers=api.auth(issued["token"]))
    assert response.status_code == 401


async def test_deleting_the_user_deletes_their_devices(api, session_factory):
    await register(api)
    async with session_factory() as db:
        await purge_user_account(db, org_id=api.org_id, user_id=api.user_id)
        await db.commit()
    from surogates.devices.store import DeviceStore

    assert await DeviceStore(session_factory).list_for_user(
        org_id=api.org_id, agent_id=AGENT_ID, user_id=api.user_id,
    ) == []
