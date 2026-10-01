"""Desktop devices: registration, presence and the device link."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
import pytest_asyncio
import uvicorn
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from surogates.db.agent_users import purge_user_account
from surogates.db.models import DeviceOperation
from surogates.devices import link as link_module
from surogates.devices.operations import (
    DeviceOperations,
    JournalRunner,
    OperationConflict,
    OperationRequest,
    operation_channel,
)
from surogates.devices.presence import DevicePresence, PRESENCE_TTL_S, presence_key
from surogates.devices.store import REVOKED_OUTCOME, DeviceStore
from surogates.devices.workspace import DeviceOperationError, DeviceWorkspaceIO
from surogates.session.store import SessionStore
from surogates.tenant.auth.jwt import create_access_token
from surogates.tenant.credentials import CredentialVault
from surogates.tools.builtin import file_ops
from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError
from surogates.tools.workspace_io import LocalWorkspaceIO
from tests.fake_laptop import FakeLaptop, perform

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
    found = await store.find_by_token(created.token)
    assert found is not None and found.id == created.device.id
    assert await store.find_by_token("not-a-device-token") is None

    rotated = await store.reauthorize(created.device.id, **owner)
    assert rotated is not None and rotated.token != created.token
    assert rotated.device.credential_generation == 2
    current = await store.find_by_token(rotated.token)
    assert current is not None
    assert (current.id, current.credential_generation, current.revoked_at) == (
        created.device.id, 2, None,
    )
    assert await store.find_by_token(created.token) is None

    await store.revoke(created.device.id, **owner)
    # A revoked device is still found by its token: the caller checks revoked_at.
    revoked = await store.find_by_token(rotated.token)
    assert revoked is not None and revoked.id == created.device.id
    assert revoked.revoked_at is not None

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
    # A handler stuck in a stalled call must fail its test, not hang the suite.
    server.force_exit = True
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
    ("case", "code"),
    [
        ("unknown", 4401),
        ("revoked", 4403),
        ("revoked, other agent", 4401),
        ("other agent", 4401),
        ("no agent", 4401),
        ("no header", 4401),
    ],
)
async def test_a_refused_device_is_closed_with_the_code_that_says_why(
    api, link_url, case, code,
):
    issued = await register(api)
    url, extra = link_url, headers(issued["token"])
    if case == "unknown":
        extra = headers("surg_dev_" + "x" * 44)
    if case.startswith("revoked"):
        await api.client.delete(f"/v1/devices/{issued['id']}", headers=api.auth())
    if case in ("other agent", "revoked, other agent"):
        url = link_url.replace(f"agent_id={AGENT_ID}", "agent_id=another-agent")
    elif case == "no agent":
        url = link_url.split("?", 1)[0]
    elif case == "no header":
        extra = {}
    async with connect(url, additional_headers=extra) as ws:
        assert await close_code(ws) == code


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
        await send(ws, {"type": "ping", "padding": "x" * (2 * 1024 * 1024 + 16)})
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
        assert await close_code(ws) == 4403


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


async def hang(*args, **kwargs):
    """A dependency call that never answers, as a blackholed Redis or Postgres does."""
    await asyncio.sleep(3600)


async def test_a_stalled_dependency_after_welcome_closes_the_link_for_a_retry(
    api, link_url, monkeypatch,
):
    monkeypatch.setattr(link_module, "DEPENDENCY_TIMEOUT_S", 0.2)
    # Every ping refreshes presence, so the first ping reaches the stalled call.
    monkeypatch.setattr(link_module, "MIN_REFRESH_INTERVAL_S", 0.0)
    issued = await register(api)
    async with linked(link_url, issued["token"]) as (ws, _):
        monkeypatch.setattr(DevicePresence, "refresh", hang)
        await send(ws, {"type": "ping"})
        assert await close_code(ws, timeout=2.0) == 1011


async def test_a_stalled_dependency_before_welcome_closes_the_link_for_a_retry(
    api, link_url, monkeypatch,
):
    monkeypatch.setattr(link_module, "DEPENDENCY_TIMEOUT_S", 0.2)
    monkeypatch.setattr(DevicePresence, "claim", hang)
    issued = await register(api)
    async with connect(link_url, additional_headers=headers(issued["token"])) as ws:
        await send(ws, {"type": "hello", "protocols": [1]})
        assert await close_code(ws, timeout=2.0) == 1011


async def test_a_revoke_from_an_old_generation_socket_leaves_the_restored_device(
    api, link_url, session_factory,
):
    issued = await register(api)
    async with linked(link_url, issued["token"]) as (old, _):
        # Rotate in the database only, so no message reaches the old socket.
        restored = await DeviceStore(session_factory).reauthorize(
            UUID(issued["id"]), org_id=api.org_id, agent_id=AGENT_ID, user_id=api.user_id,
        )
        await send(old, {"type": "revoke"})
        assert await close_code(old) == 4403
    listed = (await api.client.get("/v1/devices", headers=api.auth())).json()
    assert listed[0]["revoked_at"] is None
    async with linked(link_url, restored.token) as (_, welcome):
        assert welcome["device_id"] == issued["id"]


async def test_a_stalled_token_lookup_closes_the_link_for_a_retry(api, link_url, monkeypatch):
    monkeypatch.setattr(link_module, "DEPENDENCY_TIMEOUT_S", 0.2)
    monkeypatch.setattr(DeviceStore, "find_by_token", hang)
    issued = await register(api)
    async with connect(link_url, additional_headers=headers(issued["token"])) as ws:
        assert await close_code(ws, timeout=2.0) == 1011


async def test_a_stalled_release_still_closes_the_control_subscription(api, link_url, monkeypatch):
    monkeypatch.setattr(link_module, "DEPENDENCY_TIMEOUT_S", 0.2)
    subscribe = DevicePresence.subscribe
    opened = []

    async def recording_subscribe(self, device_id):
        pubsub = await subscribe(self, device_id)
        opened.append(pubsub)
        return pubsub

    monkeypatch.setattr(DevicePresence, "subscribe", recording_subscribe)
    monkeypatch.setattr(DevicePresence, "release", hang)
    issued = await register(api)
    async with linked(link_url, issued["token"]):
        pass

    async def closed() -> bool:
        return len(opened) == 1 and opened[0].connection is None

    await eventually(closed)


ROOT = uuid.UUID("00000000-0000-4000-8000-000000000001")


def request_for(device_id: UUID, *, ordinal: int = 1, args: dict | None = None) -> OperationRequest:
    return OperationRequest(
        device_id=device_id,
        root_session_id=ROOT,
        calling_session_id=ROOT,
        invocation_id=f"call-{uuid.uuid4()}",
        ordinal=ordinal,
        kind="which",
        args=args if args is not None else {"name": "sh"},
    )


async def stop(task: asyncio.Task) -> None:
    """Cancel a waiting operation and let it finish its cleanup."""
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def complete_pending(ops: DeviceOperations, device_id: UUID, folder, generation: int = 1) -> int:
    """Act as the laptop once: run every open operation and record its outcome."""
    done = 0
    for op in await ops.pending(device_id, generation):
        outcome = await perform(folder, op.kind, op.args)
        await ops.complete(device_id, generation, op.id, op.digest, outcome)
        done += 1
    return done


async def has_pending(ops: DeviceOperations, device_id: UUID, generation: int = 1) -> bool:
    return bool(await ops.pending(device_id, generation))


async def test_an_operation_waits_for_its_outcome(api, session_factory, redis_client, tmp_path):
    device_id = UUID((await register(api))["id"])
    ops = DeviceOperations(session_factory, redis_client)
    control = await DevicePresence(redis_client).subscribe(device_id)
    try:
        waiting = asyncio.create_task(ops.run(request_for(device_id)))
        assert (await next_control(control)).startswith("op:")
        await asyncio.sleep(0.1)
        assert not waiting.done()
        assert await complete_pending(ops, device_id, LocalWorkspaceIO(str(tmp_path))) == 1
        assert await asyncio.wait_for(waiting, 2.0) == {"ok": True}
        assert await ops.pending(device_id, 1) == []
    finally:
        await control.aclose()


async def test_a_repeated_request_gets_the_recorded_outcome(api, session_factory, redis_client, tmp_path):
    device_id = UUID((await register(api))["id"])
    ops = DeviceOperations(session_factory, redis_client)
    request = request_for(device_id)
    first = asyncio.create_task(ops.run(request))
    await eventually(lambda: has_pending(ops, device_id))
    await complete_pending(ops, device_id, LocalWorkspaceIO(str(tmp_path)))
    assert await asyncio.wait_for(first, 2.0) == {"ok": True}
    # The same tool call again, as after a worker crash: nothing new is queued.
    assert await asyncio.wait_for(ops.run(request), 1.0) == {"ok": True}
    assert await ops.pending(device_id, 1) == []


def _fields(request: OperationRequest) -> dict:
    return {name: getattr(request, name) for name in OperationRequest.__dataclass_fields__}


async def test_a_changed_request_under_the_same_ordinal_is_a_conflict(api, session_factory, redis_client):
    device_id = UUID((await register(api))["id"])
    ops = DeviceOperations(session_factory, redis_client)
    request = request_for(device_id)
    waiting = asyncio.create_task(ops.run(request))
    await eventually(lambda: has_pending(ops, device_id))
    changed = OperationRequest(**{**_fields(request), "args": {"name": "bash"}})
    with pytest.raises(OperationConflict):
        await ops.run(changed)
    await stop(waiting)


async def test_an_operation_needs_an_invocation_id():
    with pytest.raises(ValueError):
        OperationRequest(**{**_fields(request_for(uuid.uuid4())), "invocation_id": ""})


async def test_a_lost_completion_notice_is_caught_by_the_recheck(api, session_factory, redis_client, tmp_path):
    device_id = UUID((await register(api))["id"])
    worker = DeviceOperations(session_factory, redis_client, recheck_interval_s=0.2)

    class Silent:
        async def publish(self, channel, message):
            return 0

    api_side = DeviceOperations(session_factory, Silent())
    waiting = asyncio.create_task(worker.run(request_for(device_id)))
    await eventually(lambda: has_pending(worker, device_id))
    await complete_pending(api_side, device_id, LocalWorkspaceIO(str(tmp_path)))
    assert await asyncio.wait_for(waiting, 2.0) == {"ok": True}


async def test_a_redis_outage_while_waiting_does_not_fail_the_operation(
    api, session_factory, redis_client, tmp_path,
):
    device_id = UUID((await register(api))["id"])

    class Failing:
        """The worker's Redis, refusing to publish."""

        def pubsub(self):
            return redis_client.pubsub()

        async def publish(self, channel, message):
            raise RedisConnectionError("redis went away")

    worker = DeviceOperations(session_factory, Failing(), recheck_interval_s=0.2)
    waiting = asyncio.create_task(worker.run(request_for(device_id)))
    await eventually(lambda: has_pending(worker, device_id))
    # Drop every pub/sub connection, as a Redis failover would.
    await redis_client.execute_command("CLIENT", "KILL", "TYPE", "pubsub")
    api_side = DeviceOperations(session_factory, redis_client)
    await complete_pending(api_side, device_id, LocalWorkspaceIO(str(tmp_path)))
    assert await asyncio.wait_for(waiting, 3.0) == {"ok": True}


async def test_completing_checks_device_digest_and_credentials(api, session_factory, redis_client):
    first = UUID((await register(api))["id"])
    second = UUID((await register(api, name="Other laptop"))["id"])
    ops = DeviceOperations(session_factory, redis_client)
    waiting = asyncio.create_task(ops.run(request_for(first)))
    await eventually(lambda: has_pending(ops, first))
    [op] = await ops.pending(first, 1)
    assert await ops.pending(second, 1) == []

    assert await ops.complete(second, 1, op.id, op.digest, {"ok": True}) == "rejected"
    assert await ops.complete(first, 1, op.id, "0" * 64, {"ok": True}) == "rejected"
    assert await ops.complete(first, 1, uuid.uuid4(), op.digest, {"ok": True}) == "rejected"
    assert await ops.complete(first, 2, op.id, op.digest, {"ok": True}) == "stale"
    assert await ops.complete(first, 1, op.id, op.digest, {"ok": True}) == "completed"
    assert await ops.complete(first, 1, op.id, op.digest, {"ok": True}) == "duplicate"
    assert await asyncio.wait_for(waiting, 2.0) == {"ok": True}


async def test_old_credentials_are_offered_no_operations(api, session_factory, redis_client):
    device_id = UUID((await register(api))["id"])
    ops = DeviceOperations(session_factory, redis_client)
    waiting = asyncio.create_task(ops.run(request_for(device_id)))
    await eventually(lambda: has_pending(ops, device_id))
    await DeviceStore(session_factory).reauthorize(
        device_id, org_id=api.org_id, agent_id=AGENT_ID, user_id=api.user_id,
    )
    assert await ops.pending(device_id, 1) == []
    [op] = await ops.pending(device_id, 2)
    assert await ops.complete(device_id, 1, op.id, op.digest, {"ok": True}) == "stale"
    await stop(waiting)


async def test_pending_skips_operations_already_delivered(api, session_factory, redis_client):
    device_id = UUID((await register(api))["id"])
    ops = DeviceOperations(session_factory, redis_client)
    waits = [asyncio.create_task(ops.run(request_for(device_id))) for _ in range(3)]

    async def three() -> bool:
        return len(await ops.pending(device_id, 1)) == 3

    await eventually(three)
    first, second, third = await ops.pending(device_id, 1)
    assert await ops.pending(device_id, 1, exclude={first.id, second.id}) == [third]
    assert [op.id for op in await ops.pending(device_id, 1, limit=1, exclude={first.id})] == [second.id]
    for task in waits:
        await stop(task)


async def test_revoking_fails_the_devices_open_operations(api, session_factory, redis_client):
    issued = await register(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client, recheck_interval_s=0.2)
    waiting = asyncio.create_task(ops.run(request_for(device_id)))
    await eventually(lambda: has_pending(ops, device_id))
    await api.client.delete(f"/v1/devices/{issued['id']}", headers=api.auth())
    assert await asyncio.wait_for(waiting, 2.0) == {"error": {
        "type": "revoked", "message": "Local access to this computer was revoked",
    }}
    # Restoring the device does not bring the cancelled work back.
    await api.client.post(f"/v1/devices/{issued['id']}/reauthorize", headers=api.auth())
    assert await ops.pending(device_id, 2) == []


async def test_a_nul_in_an_operation_is_kept(api, session_factory, redis_client):
    device_id = UUID((await register(api))["id"])
    ops = DeviceOperations(session_factory, redis_client)
    waiting = asyncio.create_task(ops.run(request_for(device_id, args={"name": "a\x00b"})))
    await eventually(lambda: has_pending(ops, device_id))
    [op] = await ops.pending(device_id, 1)
    assert op.args == {"name": "a\x00b"}
    assert await ops.complete(device_id, 1, op.id, op.digest, {"ok": "x\x00y"}) == "completed"
    assert await asyncio.wait_for(waiting, 2.0) == {"ok": "x\x00y"}


async def test_a_completion_is_announced_on_the_operation_channel(api, session_factory, redis_client):
    device_id = UUID((await register(api))["id"])
    ops = DeviceOperations(session_factory, redis_client)
    waiting = asyncio.create_task(ops.run(request_for(device_id)))
    await eventually(lambda: has_pending(ops, device_id))
    [op] = await ops.pending(device_id, 1)
    listener = redis_client.pubsub()
    await listener.subscribe(operation_channel(op.id))
    try:
        await ops.complete(device_id, 1, op.id, op.digest, {"ok": False})
        assert await next_control(listener) == "completed"
    finally:
        await listener.aclose()
    assert await asyncio.wait_for(waiting, 2.0) == {"ok": False}


async def test_a_tool_calls_operations_are_numbered_in_order(api, session_factory, redis_client, tmp_path):
    device_id = UUID((await register(api))["id"])
    ops = DeviceOperations(session_factory, redis_client)
    folder = LocalWorkspaceIO(str(tmp_path.resolve()))
    invocation = f"call-{uuid.uuid4()}"
    wio = DeviceWorkspaceIO(
        JournalRunner(
            ops, device_id=device_id, root_session_id=ROOT, calling_session_id=ROOT,
            invocation_id=invocation,
        ),
        root=str(tmp_path.resolve()),
    )

    async def laptop() -> None:
        while True:
            await complete_pending(ops, device_id, folder)
            await asyncio.sleep(0.05)

    serving = asyncio.create_task(laptop())
    try:
        key = await asyncio.wait_for(wio.resolve("n.txt"), 5.0)
        await asyncio.wait_for(wio.write(key, b"one"), 5.0)
        assert await asyncio.wait_for(wio.read(key), 5.0) == b"one"
    finally:
        await stop(serving)
    async with session_factory() as db:
        rows = (await db.execute(
            select(DeviceOperation.ordinal, DeviceOperation.kind)
            .where(DeviceOperation.invocation_id == invocation)
            .order_by(DeviceOperation.ordinal)
        )).all()
    assert [(r.ordinal, r.kind) for r in rows] == [(1, "resolve"), (2, "write"), (3, "read")]


async def test_a_waiter_whose_device_is_deleted_gets_an_error(api, session_factory, redis_client):
    device_id = UUID((await register(api))["id"])
    ops = DeviceOperations(session_factory, redis_client, recheck_interval_s=0.2)
    waiting = asyncio.create_task(ops.run(request_for(device_id)))
    await eventually(lambda: has_pending(ops, device_id))
    async with session_factory() as db:
        await purge_user_account(db, org_id=api.org_id, user_id=api.user_id)
        await db.commit()
    with pytest.raises(DeviceOperationError):
        await asyncio.wait_for(waiting, 2.0)


async def test_an_operation_for_a_revoked_device_fails_at_once(api, session_factory, redis_client):
    issued = await register(api)
    device_id = UUID(issued["id"])
    await api.client.delete(f"/v1/devices/{issued['id']}", headers=api.auth())
    ops = DeviceOperations(session_factory, redis_client, recheck_interval_s=0.2)
    assert await asyncio.wait_for(ops.run(request_for(device_id)), 2.0) == {"error": {
        "type": "revoked", "message": "Local access to this computer was revoked",
    }}
    assert await ops.pending(device_id, 1) == []


async def test_a_reply_that_races_a_reauthorization_is_stale(
    api, session_factory, redis_client, monkeypatch,
):
    device_id = UUID((await register(api))["id"])
    ops = DeviceOperations(session_factory, redis_client)
    waiting = asyncio.create_task(ops.run(request_for(device_id)))
    await eventually(lambda: has_pending(ops, device_id))
    [op] = await ops.pending(device_id, 1)

    current = DeviceOperations._current
    rotations = []

    async def rotating(self, db, device, generation):
        """The credentials check out, then rotate before the reply is written."""
        valid = await current(self, db, device, generation)
        if not rotations:
            rotations.append(await DeviceStore(session_factory).reauthorize(
                device, org_id=api.org_id, agent_id=AGENT_ID, user_id=api.user_id,
            ))
        return valid

    with monkeypatch.context() as patched:
        patched.setattr(DeviceOperations, "_current", rotating)
        assert await ops.complete(device_id, 1, op.id, op.digest, {"ok": "forged"}) == "stale"
    assert rotations
    assert not waiting.done()
    assert [pending.id for pending in await ops.pending(device_id, 2)] == [op.id]
    await stop(waiting)


async def test_revoking_cancels_work_nobody_is_waiting_for(api, session_factory, redis_client):
    issued = await register(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client)
    request = request_for(device_id)
    waiting = asyncio.create_task(ops.run(request))
    await eventually(lambda: has_pending(ops, device_id))
    await stop(waiting)  # the worker is gone before the device is revoked
    await api.client.delete(f"/v1/devices/{issued['id']}", headers=api.auth())
    await api.client.post(f"/v1/devices/{issued['id']}/reauthorize", headers=api.auth())
    assert await ops.pending(device_id, 2) == []
    # The same tool call again gets the cancellation back and queues nothing.
    assert await asyncio.wait_for(ops.run(request), 1.0) == REVOKED_OUTCOME
    assert await ops.pending(device_id, 2) == []


def device_io(ops: DeviceOperations, device_id: UUID, folder: Path) -> DeviceWorkspaceIO:
    """A WorkspaceIO for one tool call on *folder*, through the journal."""
    return DeviceWorkspaceIO(
        JournalRunner(
            ops, device_id=device_id, root_session_id=ROOT, calling_session_id=ROOT,
            invocation_id=f"call-{uuid.uuid4()}",
        ),
        root=str(folder),
    )


@pytest_asyncio.fixture(loop_scope="session")
async def laptop_rig(api, link_url, session_factory, redis_client, tmp_path):
    """A registered device, its folder, a worker-side journal, and a disconnected fake laptop."""
    issued = await register(api)
    folder = (tmp_path / "laptop").resolve()
    folder.mkdir()
    laptop = FakeLaptop(link_url, issued["token"], LocalWorkspaceIO(str(folder)))
    rig = SimpleNamespace(
        device_id=UUID(issued["id"]),
        token=issued["token"],
        folder=folder,
        ops=DeviceOperations(session_factory, redis_client),
        laptop=laptop,
    )
    yield rig
    await laptop.disconnect()


async def _first_op(ws) -> dict:
    while True:
        frame = await receive(ws)
        if frame["type"] == "op":
            return frame


async def test_operations_run_on_the_laptop(laptop_rig):
    rig = laptop_rig
    await rig.laptop.connect()
    wio = device_io(rig.ops, rig.device_id, rig.folder)
    key = await asyncio.wait_for(wio.resolve("notes.txt"), 5.0)
    await asyncio.wait_for(wio.write(key, b"hello"), 5.0)
    assert (rig.folder / "notes.txt").read_bytes() == b"hello"
    assert await asyncio.wait_for(wio.read(key), 5.0) == b"hello"
    result = await asyncio.wait_for(wio.run("cat notes.txt", workdir=None, timeout=10), 5.0)
    assert result.output == "hello"
    assert rig.laptop.ran == ["resolve", "write", "read", "run"]

    async def all_acked() -> bool:
        return rig.laptop.acked == set(rig.laptop.outcomes)

    await eventually(all_acked)


async def test_errors_cross_the_link_as_their_own_types(laptop_rig):
    rig = laptop_rig
    await rig.laptop.connect()
    wio = device_io(rig.ops, rig.device_id, rig.folder)
    with pytest.raises(FileNotFoundError) as remote:
        await asyncio.wait_for(wio.read(str(rig.folder / "missing.txt")), 5.0)
    with pytest.raises(FileNotFoundError) as here:
        await LocalWorkspaceIO(str(rig.folder)).read(str(rig.folder / "missing.txt"))
    assert str(remote.value) == str(here.value)
    with pytest.raises(WorkspaceSandboxError):
        await asyncio.wait_for(wio.resolve("../outside.txt"), 5.0)


async def test_large_and_binary_output_crosses_the_link(laptop_rig):
    rig = laptop_rig
    await rig.laptop.connect()
    wio = device_io(rig.ops, rig.device_id, rig.folder)
    nul = await asyncio.wait_for(wio.run("printf 'a\\000b'", workdir=None, timeout=10), 5.0)
    assert nul.output == "a\x00b"
    big = await asyncio.wait_for(
        wio.run("head -c 3000000 /dev/zero | tr '\\000' x", workdir=None, timeout=30), 10.0,
    )
    assert "chars omitted by the computer" in big.output
    assert rig.laptop.connected


async def test_a_tool_handler_works_over_the_link(laptop_rig):
    rig = laptop_rig
    await rig.laptop.connect()
    written = json.loads(await asyncio.wait_for(file_ops._write_file_handler(
        {"path": "plan.md", "content": "# Plan\n"},
        workspace_io=device_io(rig.ops, rig.device_id, rig.folder),
    ), 5.0))
    assert written["status"] == "ok", written
    assert (rig.folder / "plan.md").read_text() == "# Plan\n"
    read = json.loads(await asyncio.wait_for(file_ops._read_file_handler(
        {"path": "plan.md"}, workspace_io=device_io(rig.ops, rig.device_id, rig.folder),
    ), 5.0))
    assert read["content"] == "# Plan\n"


async def test_an_operation_waits_for_the_laptop_to_connect(laptop_rig):
    rig = laptop_rig
    wio = device_io(rig.ops, rig.device_id, rig.folder)
    writing = asyncio.create_task(wio.write(str(rig.folder / "later.txt"), b"queued"))
    await asyncio.sleep(0.3)
    assert not writing.done()
    await rig.laptop.connect()
    await asyncio.wait_for(writing, 5.0)
    assert (rig.folder / "later.txt").read_bytes() == b"queued"


async def test_a_dropped_connection_does_not_run_an_operation_twice(laptop_rig):
    rig = laptop_rig
    rig.laptop.reply = False
    await rig.laptop.connect()
    wio = device_io(rig.ops, rig.device_id, rig.folder)
    running = asyncio.create_task(wio.run("echo once >> log.txt", workdir=None, timeout=10))

    async def dropped() -> bool:
        return rig.laptop.ran == ["run"] and not rig.laptop.connected

    await eventually(dropped)
    await rig.laptop.disconnect()
    rig.laptop.reply = True
    await rig.laptop.connect()
    result = await asyncio.wait_for(running, 5.0)
    assert result.returncode == 0
    assert (rig.folder / "log.txt").read_text() == "once\n"
    # Delivered twice, run once.
    assert len(rig.laptop.received) == 2 and len(set(rig.laptop.received)) == 1
    assert rig.laptop.ran == ["run"]


async def test_a_lost_announcement_is_delivered_at_a_ping(laptop_rig, session_factory, redis_client):
    rig = laptop_rig
    await rig.laptop.connect()

    class Silent:
        """The worker's Redis, with its announcement lost."""

        def pubsub(self):
            return redis_client.pubsub()

        async def publish(self, channel, message):
            return 0

    wio = device_io(DeviceOperations(session_factory, Silent()), rig.device_id, rig.folder)
    assert await asyncio.wait_for(wio.which("sh"), 5.0) is True


async def test_a_reply_for_another_devices_operation_closes_the_link(api, link_url, laptop_rig):
    rig = laptop_rig
    other = await register(api, name="Other laptop")
    waiting = asyncio.create_task(device_io(rig.ops, rig.device_id, rig.folder).which("sh"))
    await eventually(lambda: has_pending(rig.ops, rig.device_id))
    [op] = await rig.ops.pending(rig.device_id, 1)
    async with linked(link_url, other["token"]) as (ws, _):
        await send(ws, {"type": "op_result", "id": str(op.id), "digest": op.digest, "outcome": {"ok": True}})
        assert await close_code(ws) == 4400
    assert await rig.ops.pending(rig.device_id, 1) != []
    await stop(waiting)


@pytest.mark.parametrize("change", [
    {"digest": "0" * 64},
    {"digest": 7},
    {"id": "not-a-uuid"},
    {"outcome": {"neither": True}},
    {"outcome": {"ok": True, "error": {"type": "os"}}},
    {"outcome": {"error": "not an object"}},
])
async def test_a_bad_reply_closes_the_link_and_records_nothing(laptop_rig, link_url, change):
    rig = laptop_rig
    waiting = asyncio.create_task(device_io(rig.ops, rig.device_id, rig.folder).which("sh"))
    async with linked(link_url, rig.token) as (ws, _):
        op = await _first_op(ws)
        await send(ws, {
            "type": "op_result", "id": op["id"], "digest": op["digest"], "outcome": {"ok": True},
            **change,
        })
        assert await close_code(ws) == 4400
    assert await rig.ops.pending(rig.device_id, 1) != []
    await stop(waiting)


async def test_a_repeated_reply_is_acknowledged_again(laptop_rig, link_url):
    rig = laptop_rig
    waiting = asyncio.create_task(device_io(rig.ops, rig.device_id, rig.folder).which("sh"))
    async with linked(link_url, rig.token) as (ws, _):
        op = await _first_op(ws)
        reply = {"type": "op_result", "id": op["id"], "digest": op["digest"], "outcome": {"ok": True}}
        await send(ws, reply)
        assert await receive(ws) == {"type": "op_ack", "id": op["id"]}
        await send(ws, reply)
        assert await receive(ws) == {"type": "op_ack", "id": op["id"]}
    assert await asyncio.wait_for(waiting, 2.0) is True


async def test_old_credentials_get_no_operations_and_cannot_reply(
    api, laptop_rig, link_url, session_factory,
):
    rig = laptop_rig
    async with linked(link_url, rig.token) as (ws, _):
        # Rotate in the database only, as if the rotation notice were lost.
        await DeviceStore(session_factory).reauthorize(
            rig.device_id, org_id=api.org_id, agent_id=AGENT_ID, user_id=api.user_id,
        )
        waiting = asyncio.create_task(device_io(rig.ops, rig.device_id, rig.folder).which("sh"))
        await eventually(lambda: has_pending(rig.ops, rig.device_id, 2))
        await send(ws, {"type": "ping"})
        assert await receive(ws) == {"type": "pong"}
        [op] = await rig.ops.pending(rig.device_id, 2)
        await send(ws, {"type": "op_result", "id": str(op.id), "digest": op.digest, "outcome": {"ok": True}})
        assert await close_code(ws) == 4403
    assert await rig.ops.pending(rig.device_id, 2) != []
    await stop(waiting)


async def test_operations_go_to_the_connection_that_took_over(laptop_rig, link_url):
    rig = laptop_rig
    async with linked(link_url, rig.token) as (old, _):
        async with linked(link_url, rig.token) as (new, _):
            assert await close_code(old) == 4409
            waiting = asyncio.create_task(device_io(rig.ops, rig.device_id, rig.folder).which("sh"))
            op = await _first_op(new)
            assert op["kind"] == "which"
            await stop(waiting)
