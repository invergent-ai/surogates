"""Desktop devices: registration, presence and the device link."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

import pytest
import pytest_asyncio
import uvicorn
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from surogates.db.agent_users import purge_user_account
from surogates.devices import link as link_module
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


@pytest.mark.parametrize("failing", ["subscribe", "get_message"])
async def test_subscribing_closes_the_pubsub_when_it_fails(redis_client, monkeypatch, failing):
    closed: list[bool] = []
    real_pubsub = redis_client.pubsub

    def tracked_pubsub():
        pubsub = real_pubsub()
        real_aclose = pubsub.aclose

        async def broken(*args, **kwargs):
            raise ConnectionError("redis went away")

        async def aclose():
            closed.append(True)
            await real_aclose()

        monkeypatch.setattr(pubsub, failing, broken)
        monkeypatch.setattr(pubsub, "aclose", aclose)
        return pubsub

    monkeypatch.setattr(redis_client, "pubsub", tracked_pubsub)
    with pytest.raises(ConnectionError):
        await DevicePresence(redis_client).subscribe(uuid.uuid4())
    assert closed == [True]


@pytest_asyncio.fixture(loop_scope="session")
async def link_url(api):
    """The app, served by a real uvicorn server in the test's own event loop."""
    server = uvicorn.Server(uvicorn.Config(
        api.app, host="127.0.0.1", port=0, lifespan="off", log_config=None,
    ))
    task = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started:
            break
        await asyncio.sleep(0.01)
    assert server.started, "uvicorn did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"ws://127.0.0.1:{port}/api/v1/devices/connect?agent_id={AGENT_ID}"
    server.should_exit = True
    await task


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def send(ws, frame: dict) -> None:
    await ws.send(json.dumps(frame))


async def receive(ws, timeout: float = 5.0) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def close_code(ws, timeout: float = 5.0) -> int:
    with pytest.raises(ConnectionClosed) as closed:
        while True:
            await asyncio.wait_for(ws.recv(), timeout)
    return closed.value.rcvd.code


@asynccontextmanager
async def linked(url: str, token: str):
    """A device connection past the handshake."""
    async with connect(url, additional_headers=headers(token)) as ws:
        await send(ws, {"type": "hello", "protocols": [1]})
        welcome = await receive(ws)
        assert welcome["type"] == "welcome", welcome
        yield ws, welcome


async def online(api) -> bool:
    return (await api.client.get("/v1/devices", headers=api.auth())).json()[0]["online"]


async def offline(api) -> bool:
    return not await online(api)


async def eventually(check, timeout: float = 3.0) -> None:
    for _ in range(int(timeout / 0.05)):
        if await check():
            return
        await asyncio.sleep(0.05)
    assert await check()


@pytest.mark.parametrize(
    "case", ["unknown", "revoked", "other agent", "no agent", "no header"],
)
async def test_a_refused_device_is_closed_with_4401(api, link_url, case):
    issued = await register(api)
    url, extra = link_url, headers(issued["token"])
    if case == "unknown":
        extra = headers("surg_dev_" + "x" * 44)
    elif case == "revoked":
        await api.client.delete(f"/v1/devices/{issued['id']}", headers=api.auth())
    elif case == "other agent":
        url = link_url.replace(f"agent_id={AGENT_ID}", "agent_id=another-agent")
    elif case == "no agent":
        url = link_url.split("?", 1)[0]
    elif case == "no header":
        extra = {}
    async with connect(url, additional_headers=extra) as ws:
        assert await close_code(ws) == 4401


async def test_a_connected_device_is_online_until_it_leaves(api, link_url):
    issued = await register(api)
    async with linked(link_url, issued["token"]) as (ws, welcome):
        assert welcome == {
            "type": "welcome",
            "protocol": 1,
            "device_id": issued["id"],
            "org_id": str(api.org_id),
            "agent_id": AGENT_ID,
            "user_id": str(api.user_id),
            "name": "Flavius's ThinkPad",
            "heartbeat_s": 15,
        }
        assert await online(api) is True
        await send(ws, {"type": "ping"})
        assert await receive(ws) == {"type": "pong"}

    await eventually(lambda: offline(api))
    listed = (await api.client.get("/v1/devices", headers=api.auth())).json()
    assert listed[0]["last_seen_at"] is not None


async def test_an_unsupported_protocol_is_refused(api, link_url):
    issued = await register(api)
    async with connect(link_url, additional_headers=headers(issued["token"])) as ws:
        await send(ws, {"type": "hello", "protocols": [2]})
        assert await receive(ws) == {
            "type": "error", "code": "unsupported_protocol", "supported": [1],
        }
        assert await close_code(ws) == 4400


@pytest.mark.parametrize("frame", ["not json", "[1]", json.dumps({"type": "ping"})])
async def test_a_first_frame_that_is_not_hello_is_a_protocol_error(api, link_url, frame):
    issued = await register(api)
    async with connect(link_url, additional_headers=headers(issued["token"])) as ws:
        await ws.send(frame)
        assert await close_code(ws) == 4400


async def test_an_oversized_frame_is_a_protocol_error(api, link_url):
    issued = await register(api)
    async with linked(link_url, issued["token"]) as (ws, _):
        await send(ws, {"type": "ping", "padding": "x" * 70_000})
        assert await close_code(ws) == 4400


async def test_no_hello_in_time_is_a_protocol_error(api, link_url, monkeypatch):
    monkeypatch.setattr(link_module, "HELLO_TIMEOUT_S", 0.2)
    issued = await register(api)
    async with connect(link_url, additional_headers=headers(issued["token"])) as ws:
        assert await close_code(ws) == 4400


async def test_a_silent_device_is_closed(api, link_url, monkeypatch):
    monkeypatch.setattr(link_module, "IDLE_TIMEOUT_S", 0.3)
    issued = await register(api)
    async with linked(link_url, issued["token"]) as (ws, _):
        assert await close_code(ws) == 4408


async def test_a_new_connection_supersedes_the_old_one(api, link_url):
    issued = await register(api)
    async with linked(link_url, issued["token"]) as (old, _):
        async with linked(link_url, issued["token"]) as (new, _):
            assert await close_code(old) == 4409
            # The old connection's cleanup must leave the new one's presence alone.
            await asyncio.sleep(0.2)
            assert await online(api) is True
            await send(new, {"type": "ping"})
            assert await receive(new) == {"type": "pong"}


async def test_two_connections_opened_together_leave_exactly_one(api, link_url):
    issued = await register(api)

    async def open_link():
        ws = await connect(link_url, additional_headers=headers(issued["token"]))
        await send(ws, {"type": "hello", "protocols": [1]})
        assert (await receive(ws))["type"] == "welcome"
        return ws

    async def outcome(ws) -> str:
        try:
            await send(ws, {"type": "ping"})
            reply = await receive(ws, timeout=2.0)
            return "live" if reply == {"type": "pong"} else f"unexpected {reply}"
        except ConnectionClosed as closed:
            return str(closed.rcvd.code)

    first, second = await asyncio.gather(open_link(), open_link())
    try:
        await asyncio.sleep(0.5)
        assert sorted([await outcome(first), await outcome(second)]) == ["4409", "live"]
    finally:
        await first.close()
        await second.close()


async def test_revocation_closes_the_link(api, link_url):
    issued = await register(api)
    async with linked(link_url, issued["token"]) as (ws, _):
        await api.client.delete(f"/v1/devices/{issued['id']}", headers=api.auth())
        assert await close_code(ws) == 4403


async def test_a_late_revocation_message_leaves_a_restored_device_connected(
    api, link_url, redis_client,
):
    issued = await register(api)
    await api.client.delete(f"/v1/devices/{issued['id']}", headers=api.auth())
    restored = (await api.client.post(
        f"/v1/devices/{issued['id']}/reauthorize", headers=api.auth(),
    )).json()
    async with linked(link_url, restored["token"]) as (ws, _):
        # Generation 1's revocation, delivered after generation 2 connected.
        await DevicePresence(redis_client).publish(UUID(issued["id"]), "revoked:1")
        await asyncio.sleep(0.3)
        await send(ws, {"type": "ping"})
        assert await receive(ws) == {"type": "pong"}


async def test_reauthorization_closes_the_old_link_and_admits_the_new_token(api, link_url):
    issued = await register(api)
    async with linked(link_url, issued["token"]) as (ws, _):
        response = await api.client.post(
            f"/v1/devices/{issued['id']}/reauthorize", headers=api.auth(),
        )
        assert await close_code(ws) == 4403
    async with connect(link_url, additional_headers=headers(issued["token"])) as ws:
        assert await close_code(ws) == 4401
    async with linked(link_url, response.json()["token"]) as (_, welcome):
        assert welcome["device_id"] == issued["id"]


async def test_the_app_can_revoke_its_own_device(api, link_url):
    issued = await register(api)
    async with linked(link_url, issued["token"]) as (ws, _):
        await send(ws, {"type": "revoke"})
        assert await close_code(ws) == 4403
    listed = (await api.client.get("/v1/devices", headers=api.auth())).json()
    assert listed[0]["revoked_at"] is not None
    async with connect(link_url, additional_headers=headers(issued["token"])) as ws:
        assert await close_code(ws) == 4401


async def test_a_lost_revocation_message_is_caught_at_the_next_check(
    api, link_url, session_factory, monkeypatch,
):
    monkeypatch.setattr(link_module, "LAST_SEEN_INTERVAL_S", 0.0)
    issued = await register(api)
    async with linked(link_url, issued["token"]) as (ws, _):
        # Revoke in the database only, as if the control message were lost.
        await DeviceStore(session_factory).revoke(
            UUID(issued["id"]), org_id=api.org_id, agent_id=AGENT_ID, user_id=api.user_id,
        )
        await send(ws, {"type": "ping"})
        assert await close_code(ws) == 4403


async def test_deleting_the_user_closes_the_link(api, link_url, session_factory, monkeypatch):
    monkeypatch.setattr(link_module, "LAST_SEEN_INTERVAL_S", 0.0)
    issued = await register(api)
    async with linked(link_url, issued["token"]) as (ws, _):
        async with session_factory() as db:
            await purge_user_account(db, org_id=api.org_id, user_id=api.user_id)
            await db.commit()
        await send(ws, {"type": "ping"})
        assert await close_code(ws) == 4403


async def test_a_rotation_before_the_subscription_still_closes_the_link(
    api, link_url, session_factory, monkeypatch,
):
    issued = await register(api)
    subscribe = DevicePresence.subscribe

    async def rotate_then_subscribe(self, device_id):
        # Rotate in the database only, before this connection listens: no
        # message will reach it, so only the post-claim check can catch it.
        await DeviceStore(session_factory).reauthorize(
            device_id, org_id=api.org_id, agent_id=AGENT_ID, user_id=api.user_id,
        )
        return await subscribe(self, device_id)

    monkeypatch.setattr(DevicePresence, "subscribe", rotate_then_subscribe)
    async with connect(link_url, additional_headers=headers(issued["token"])) as ws:
        await send(ws, {"type": "hello", "protocols": [1]})
        assert await close_code(ws) == 4403


async def test_a_redis_failure_closes_the_link_for_a_retry(api, link_url, redis_client):
    issued = await register(api)
    async with linked(link_url, issued["token"]) as (ws, _):
        # Drop every pub/sub connection, as a Redis failover would.
        await redis_client.execute_command("CLIENT", "KILL", "TYPE", "pubsub")
        assert await close_code(ws) == 1011
    await eventually(lambda: offline(api))
