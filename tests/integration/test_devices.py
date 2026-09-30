"""Desktop devices: registration, presence and the device link."""

from __future__ import annotations

import asyncio
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
from surogates.devices.presence import DevicePresence, PRESENCE_TTL_S, presence_key
from surogates.devices.store import DeviceStore
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
    _, other_token = await add_user(session_factory, api.org_id)
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
    assert await DeviceStore(session_factory).list_for_user(
        org_id=api.org_id, agent_id=AGENT_ID, user_id=api.user_id,
    ) == []


async def test_a_device_belongs_to_one_agent(api, monkeypatch):
    from surogates.runtime import agent_runtime_context_dep, build_agent_runtime_context

    issued = await register(api)
    device_url = f"/v1/devices/{issued['id']}"

    monkeypatch.setitem(
        api.app.dependency_overrides,
        agent_runtime_context_dep,
        lambda: build_agent_runtime_context({
            "agent_id": "agent-other",
            "org_id": str(api.org_id),
            "project_id": "test-project",
            "enabled": True,
            "version": 1,
            "storage_key_prefix": "",
        }),
    )
    assert (await api.client.get("/v1/devices", headers=api.auth())).json() == []
    assert (await api.client.delete(device_url, headers=api.auth())).status_code == 404
    assert (await api.client.post(f"{device_url}/reauthorize", headers=api.auth())).status_code == 404

    monkeypatch.undo()
    listed = (await api.client.get("/v1/devices", headers=api.auth())).json()
    assert [(d["id"], d["revoked_at"], d["token_prefix"]) for d in listed] == [
        (issued["id"], None, issued["token_prefix"]),
    ]


async def test_token_rotation_and_revocation_at_the_store(api, session_factory):
    store = DeviceStore(session_factory)
    owner = {"org_id": api.org_id, "agent_id": AGENT_ID, "user_id": api.user_id}

    created = await store.create(name="Desk", **owner)
    assert created.device.credential_generation == 1
    found = await store.get_by_token(created.token)
    assert found is not None and found.id == created.device.id
    assert await store.get_by_token("not-a-device-token") is None

    rotated = await store.reauthorize(created.device.id, **owner)
    assert rotated is not None and rotated.token != created.token
    assert rotated.device.credential_generation == 2
    current = await store.get_by_token(rotated.token)
    assert current is not None
    assert (current.id, current.credential_generation, current.revoked_at) == (
        created.device.id, 2, None,
    )
    assert await store.get_by_token(created.token) is None

    await store.revoke(created.device.id, **owner)
    assert await store.get_by_token(rotated.token) is None

    touched = await store.touch(created.device.id)
    assert touched is not None
    assert touched.last_seen_at is not None
    assert touched.revoked_at is not None
    assert await store.touch(uuid.uuid4()) is None


async def next_control(pubsub, timeout: float = 2.0) -> str:
    async def read() -> str:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
            if message is not None:
                return message["data"].decode()

    return await asyncio.wait_for(read(), timeout)


async def test_presence_belongs_to_the_connection_that_claimed_it(redis_client):
    presence = DevicePresence(redis_client)
    device_id = uuid.uuid4()

    await presence.claim(device_id, "pod-a:1")
    ttl = await redis_client.ttl(presence_key(device_id))
    assert PRESENCE_TTL_S - 5 < ttl <= PRESENCE_TTL_S
    assert await presence.refresh(device_id, "pod-a:1")

    await presence.claim(device_id, "pod-b:2")
    assert not await presence.refresh(device_id, "pod-a:1")
    assert not await presence.holds(device_id, "pod-a:1")
    assert await presence.holds(device_id, "pod-b:2")
    await presence.release(device_id, "pod-a:1")
    assert await presence.online([device_id]) == {device_id}

    await presence.release(device_id, "pod-b:2")
    assert await presence.online([device_id]) == set()
    # An expired claim is no one's: the connection that still holds the socket takes it back.
    assert await presence.refresh(device_id, "pod-a:1")
    assert await presence.online([device_id]) == {device_id}


async def test_claiming_tells_older_connections_to_close(redis_client):
    presence = DevicePresence(redis_client)
    device_id = uuid.uuid4()
    pubsub = await presence.subscribe(device_id)
    try:
        await presence.claim(device_id, "pod-a:1")
        assert await next_control(pubsub) == "superseded:pod-a:1"
    finally:
        await pubsub.aclose()


async def test_the_list_shows_which_devices_are_online(api, redis_client):
    issued = await register(api)
    device_id = UUID(issued["id"])
    listed = (await api.client.get("/v1/devices", headers=api.auth())).json()
    assert listed[0]["online"] is False

    await DevicePresence(redis_client).claim(device_id, "pod-a:1")
    listed = (await api.client.get("/v1/devices", headers=api.auth())).json()
    assert listed[0]["online"] is True


async def test_revoking_and_reauthorizing_notify_the_connection(api, redis_client):
    issued = await register(api)
    pubsub = await DevicePresence(redis_client).subscribe(UUID(issued["id"]))
    try:
        await api.client.delete(f"/v1/devices/{issued['id']}", headers=api.auth())
        assert await next_control(pubsub) == "revoked:1"
        await api.client.post(f"/v1/devices/{issued['id']}/reauthorize", headers=api.auth())
        assert await next_control(pubsub) == "rotated:2"
    finally:
        await pubsub.aclose()


async def test_a_failed_notification_does_not_fail_the_change(api, monkeypatch):
    issued = await register(api)

    async def broken_publish(self, device_id, message):
        raise ConnectionError("redis went away")

    monkeypatch.setattr(DevicePresence, "publish", broken_publish)
    revoked = await api.client.delete(f"/v1/devices/{issued['id']}", headers=api.auth())
    assert revoked.status_code == 204
    restored = await api.client.post(
        f"/v1/devices/{issued['id']}/reauthorize", headers=api.auth(),
    )
    # The new token must reach the user: the old one no longer works.
    assert restored.status_code == 200
    assert restored.json()["token"].startswith("surg_dev_")
