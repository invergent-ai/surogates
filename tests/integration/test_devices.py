"""Desktop devices: registration, presence and the device link."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pytest
import pytest_asyncio
import uvicorn
from asyncpg.exceptions import InternalClientError
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError, InterfaceError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Delete
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from surogates.db.agent_users import purge_user_account
from surogates.db.models import Device, DeviceOperation, Event
from surogates.devices import link as link_module
from surogates.devices import operations as operations_module
from surogates.devices import workspace as workspace_module
from surogates.devices.binding import BIND, Binding, binding_of, device_of
from surogates.devices.browser import DeviceBrowserClient
from surogates.devices.operations import (
    CANCELLED_OUTCOME,
    DeviceOperations,
    JournalRunner,
    OperationConflict,
    OperationRequest,
    operation_channel,
)
from surogates.devices.presence import DevicePresence, PRESENCE_TTL_S, presence_key
from surogates.devices.sandbox import INTERRUPTED, INTERRUPTED_IN_BROWSER
from surogates.devices.store import REVOKED_OUTCOME, DeviceStore
from surogates.devices.waits import DeviceWaitNotice
from surogates.devices.workspace import WALK_MARGIN_NS, DeviceOperationError, DeviceWorkspaceIO
from surogates.governance.policy import GovernanceGate
from surogates.harness.device_replay import replay_unanswered
from surogates.harness import loop_artifact_completion
from surogates.harness.loop_artifact_completion import ArtifactCompletionMixin
from surogates.harness import loop_context_replay
from surogates.harness.loop_context_replay import ContextReplayMixin
from surogates.harness.prompt_cache import SystemPromptCache
from surogates.harness.tool_exec import execute_single_tool
from surogates.runtime.turn_slots import TurnSlots, current_turn
from surogates.session.events import EventType
from surogates.session.provisioning import create_child_session
from surogates.session.store import SessionStore
from surogates.tenant.auth.jwt import create_access_token
from surogates.tenant.credentials import CredentialVault
from surogates.tools.builtin import file_ops
from surogates.tools.utils import process_registry
from surogates.tools.registry import ToolRegistry, ToolSchema
from surogates.tools.router import TOOL_LOCATIONS, ToolLocation
from surogates.tools.runtime import ToolRuntime
from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError
from surogates.tools.workspace_io import NUL_REFUSED, LocalWorkspaceIO
from tests.fake_laptop import FakeLaptop, perform
from tests.test_turn_slots import as_tool_call, held_turn

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
    # Signed in just now: adding a computer needs a recent sign-in.
    return user_id, create_access_token(org_id, user_id, {"sessions:read", "sessions:write"}, auth_time=int(time.time()))


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


async def test_the_list_says_when_a_computer_was_added_and_when_it_was_reauthorized(api):
    issued = await register(api)
    [listed] = (await api.client.get("/v1/devices", headers=api.auth())).json()
    assert (listed["created_at"], listed["reauthorized_at"]) == (issued["created_at"], None)

    response = await api.client.post(f"/v1/devices/{issued['id']}/reauthorize", headers=api.auth())
    assert response.status_code == 200, response.text
    restored = response.json()
    assert restored["reauthorized_at"] is not None
    [listed] = (await api.client.get("/v1/devices", headers=api.auth())).json()
    assert (listed["created_at"], listed["reauthorized_at"]) == (issued["created_at"], restored["reauthorized_at"])
    # UTC, with its zone: a browser reads a time without one as its own local time.
    added, reauthorized = (datetime.fromisoformat(listed[key]) for key in ("created_at", "reauthorized_at"))
    assert added.utcoffset() == reauthorized.utcoffset() == timedelta(0)
    assert reauthorized >= added


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
    """A device connection past the handshake, taking frames as large as the app takes."""
    async with connect(url, additional_headers=headers(token), max_size=4 * link_module.MAX_FRAME_CHARS) as ws:
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


NONCE = "binding-nonce-0001"
FOLDER = "/nonexistent/Surogate/agent-devices/notes"


async def device_session(
    api: Api, device_id: UUID, *, user_id: UUID | None = None, folder: str = FOLDER,
) -> UUID:
    """A root session that works on *device_id*, with no binding yet."""
    session = await SessionStore(api.app.state.session_factory).create_session(
        user_id=user_id or api.user_id,
        org_id=api.org_id,
        agent_id=AGENT_ID,
        channel="web",
        config={
            "execution": {"kind": "device", "device_id": str(device_id)},
            "workspace_path": folder,
            # What create_child_session needs from a parent.
            "storage_bucket": "test-bucket",
            "storage_key_prefix": "",
        },
    )
    return session.id


async def bound_device(api: Api, name: str = "Flavius's ThinkPad") -> tuple[dict, UUID]:
    """A registered device, and a root session its app has bound."""
    issued = await register(api, name)
    device_id = UUID(issued["id"])
    root = await device_session(api, device_id)
    ops = DeviceOperations(api.app.state.session_factory, api.app.state.redis)
    await ops.bind(session_id=root, device_id=device_id, folder=FOLDER, nonce=NONCE)
    [bind] = await ops.pending(device_id, 1)
    assert await ops.complete(device_id, 1, bind.id, bind.digest, {"ok": None}) == "completed"
    return issued, root


async def binding(api: Api, session_id: UUID) -> Binding:
    async with api.app.state.session_factory() as db:
        return await binding_of(db, session_id)


def binding_is(api: Api, session_id: UUID, state: str):
    async def check() -> bool:
        return (await binding(api, session_id)).state == state
    return check


def request_for(
    device_id: UUID, root: UUID, *, ordinal: int = 1, args: dict | None = None,
) -> OperationRequest:
    return OperationRequest(
        device_id=device_id,
        root_session_id=root,
        calling_session_id=root,
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
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client)
    control = await DevicePresence(redis_client).subscribe(device_id)
    try:
        waiting = asyncio.create_task(ops.run(request_for(device_id, root)))
        assert (await next_control(control)).startswith("op:")
        await asyncio.sleep(0.1)
        assert not waiting.done()
        assert await complete_pending(ops, device_id, LocalWorkspaceIO(str(tmp_path))) == 1
        assert await asyncio.wait_for(waiting, 2.0) == {"ok": True}
        assert await ops.pending(device_id, 1) == []
    finally:
        await control.aclose()


async def test_a_quick_answer_keeps_the_turns_slots(api, session_factory, redis_client, tmp_path, monkeypatch):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    # Far longer than the test takes: a slow machine cannot outlast the grace.
    monkeypatch.setattr(operations_module, "WAIT_GRACE_S", 30)
    ops = DeviceOperations(session_factory, redis_client)
    slots, semaphore, gate = await held_turn()
    token = current_turn.set(slots)
    try:
        async with as_tool_call(slots):
            waiting = asyncio.create_task(ops.run(request_for(device_id, root)))
            await eventually(lambda: has_pending(ops, device_id))
            assert await complete_pending(ops, device_id, LocalWorkspaceIO(str(tmp_path))) == 1
            assert await asyncio.wait_for(waiting, 2.0) == {"ok": True}
    finally:
        current_turn.reset(token)
    assert gate.calls == [], "an operation answered within the grace churned the turn's slots"


async def test_a_slow_operation_gives_the_turns_slots_back(
    api, session_factory, redis_client, tmp_path, monkeypatch,
):
    # raising=False: the attribute does not exist before the change.
    monkeypatch.setattr(operations_module, "WAIT_GRACE_S", 0.1, raising=False)
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client, recheck_interval_s=0.2)
    slots, semaphore, gate = await held_turn()

    async def released() -> bool:
        return not semaphore.locked()

    token = current_turn.set(slots)
    try:
        async with as_tool_call(slots):
            waiting = asyncio.create_task(ops.run(request_for(device_id, root)))
            await eventually(released)
            assert gate.held == 0
            assert await complete_pending(ops, device_id, LocalWorkspaceIO(str(tmp_path))) == 1
            assert await asyncio.wait_for(waiting, 3.0) == {"ok": True}
            assert semaphore.locked() and gate.held == 1
    finally:
        current_turn.reset(token)


async def test_a_repeated_request_gets_the_recorded_outcome(api, session_factory, redis_client, tmp_path):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client)
    request = request_for(device_id, root)
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
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client)
    request = request_for(device_id, root)
    waiting = asyncio.create_task(ops.run(request))
    await eventually(lambda: has_pending(ops, device_id))
    changed = OperationRequest(**{**_fields(request), "args": {"name": "bash"}})
    with pytest.raises(OperationConflict):
        await ops.run(changed)
    await stop(waiting)


async def test_an_operation_from_a_worker_that_lost_the_session_is_refused(api, session_factory, redis_client):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    store = SessionStore(session_factory)
    stale = await store.try_acquire_lease(root, "worker-a", ttl_seconds=60)
    await store.release_lease(root, stale.lease_token)
    current = await store.try_acquire_lease(root, "worker-b", ttl_seconds=60)
    ops = DeviceOperations(session_factory, redis_client)
    request = request_for(device_id, root)

    with pytest.raises(DeviceOperationError, match="Another worker runs this session now"):
        await asyncio.wait_for(
            ops.run(OperationRequest(**{**_fields(request), "lease_token": str(stale.lease_token)})), 5.0,
        )
    assert await operation_rows(session_factory, device_id) == []

    waiting = asyncio.create_task(
        ops.run(OperationRequest(**{**_fields(request), "lease_token": str(current.lease_token)})),
    )
    await eventually(lambda: has_pending(ops, device_id))
    await stop(waiting)


async def test_an_operation_needs_an_invocation_id():
    with pytest.raises(ValueError):
        OperationRequest(**{**_fields(request_for(uuid.uuid4(), uuid.uuid4())), "invocation_id": ""})


async def test_a_lost_completion_notice_is_caught_by_the_recheck(api, session_factory, redis_client, tmp_path):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    worker = DeviceOperations(session_factory, redis_client, recheck_interval_s=0.2)

    class Silent:
        async def publish(self, channel, message):
            return 0

    api_side = DeviceOperations(session_factory, Silent())
    waiting = asyncio.create_task(worker.run(request_for(device_id, root)))
    await eventually(lambda: has_pending(worker, device_id))
    await complete_pending(api_side, device_id, LocalWorkspaceIO(str(tmp_path)))
    assert await asyncio.wait_for(waiting, 2.0) == {"ok": True}


async def test_a_redis_outage_while_waiting_does_not_fail_the_operation(
    api, session_factory, redis_client, tmp_path,
):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])

    class Failing:
        """The worker's Redis, refusing to publish."""

        def pubsub(self):
            return redis_client.pubsub()

        async def publish(self, channel, message):
            raise RedisConnectionError("redis went away")

    worker = DeviceOperations(session_factory, Failing(), recheck_interval_s=0.2)
    waiting = asyncio.create_task(worker.run(request_for(device_id, root)))
    await eventually(lambda: has_pending(worker, device_id))
    # Drop every pub/sub connection, as a Redis failover would.
    await redis_client.execute_command("CLIENT", "KILL", "TYPE", "pubsub")
    api_side = DeviceOperations(session_factory, redis_client)
    await complete_pending(api_side, device_id, LocalWorkspaceIO(str(tmp_path)))
    assert await asyncio.wait_for(waiting, 3.0) == {"ok": True}


def a_dropped_connection() -> OperationalError:
    """What a failover or reset makes the next database call raise."""
    return OperationalError(
        "SELECT ...", {}, ConnectionResetError("connection was closed in the middle of operation"),
    )


async def test_a_database_blip_while_waiting_does_not_fail_the_operation(
    laptop_rig, session_factory, redis_client, monkeypatch,
):
    rig = laptop_rig
    real = DeviceOperations._outcome
    rechecks = []

    async def flaky(self, operation_id):
        rechecks.append(operation_id)
        if len(rechecks) == 2:  # the wait's second recheck
            raise a_dropped_connection()
        return await real(self, operation_id)

    monkeypatch.setattr(DeviceOperations, "_outcome", flaky)
    worker = DeviceOperations(session_factory, redis_client, recheck_interval_s=0.2)
    writing = asyncio.create_task(
        device_io(worker, rig.device_id, rig.root, rig.folder).write(str(rig.folder / "deploy.txt"), b"ran\n"),
    )

    async def blipped() -> bool:
        return len(rechecks) >= 2

    await eventually(blipped)
    # The laptop was already given the operation; the worker must still be there for its answer.
    await rig.laptop.connect()
    await asyncio.wait_for(writing, 5.0)
    assert (rig.folder / "deploy.txt").read_bytes() == b"ran\n"
    assert rig.laptop.ran == ["write"]
    assert len(rechecks) >= 3


async def test_a_database_blip_while_recording_does_not_fail_the_operation(
    api, session_factory, redis_client, tmp_path, monkeypatch,
):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    real = DeviceOperations._record
    attempts = []

    async def flaky(self, request):
        attempts.append(request)
        if len(attempts) == 1:
            raise a_dropped_connection()
        return await real(self, request)

    monkeypatch.setattr(DeviceOperations, "_record", flaky)
    ops = DeviceOperations(session_factory, redis_client, recheck_interval_s=0.2)
    waiting = asyncio.create_task(ops.run(request_for(device_id, root)))
    await eventually(lambda: has_pending(ops, device_id))
    await complete_pending(ops, device_id, LocalWorkspaceIO(str(tmp_path)))
    assert await asyncio.wait_for(waiting, 3.0) == {"ok": True}
    assert len(attempts) == 2


@pytest.mark.parametrize(
    ("error", "blip"),
    [
        (a_dropped_connection(), True),
        (InterfaceError("SELECT ...", {}, Exception("connection is closed")), True),
        (DBAPIError("SELECT ...", {}, Exception("terminating connection"), connection_invalidated=True), True),
        (ConnectionRefusedError("the primary is restarting"), True),
        (TimeoutError("pool timeout"), True),
        # What the driver says of a connection whose backend ended between two of a call's statements.
        (InternalClientError("cannot switch to state 11; another operation (2) is in progress"), True),
        # Its other faults never go away: asked again, the same answer for ever.
        (InternalClientError("no encoder for OID 16385"), False),
        (InternalClientError("Bind: expected a sequence, got NoneType"), False),
        (DBAPIError("SELECT ...", {}, Exception("some other failure")), False),
        (IntegrityError("INSERT ...", {}, Exception("duplicate key")), False),
        (ProgrammingError("SELECT ...", {}, Exception("no such column")), False),
        (OperationConflict("changed"), False),
        (ValueError("bad"), False),
    ],
    ids=lambda case: f"{type(case).__name__} {str(case)[:12]}" if not isinstance(case, bool) else str(case),
)
async def test_only_a_database_out_of_reach_is_worth_waiting_out(error, blip):
    assert operations_module._database_unavailable(error) is blip


async def test_waiting_out_the_database_says_what_it_said(session_factory, redis_client, caplog):
    ops = DeviceOperations(session_factory, redis_client, recheck_interval_s=0.01)
    said = [InternalClientError("cannot switch to state 11; another operation (2) is in progress")]

    async def call():
        if said:
            raise said.pop()
        return "recorded"

    with caplog.at_level(logging.WARNING, logger="surogates.devices.operations"):
        assert await ops._while_database_recovers("recording", call) == "recorded"
    # The message, not only the kind of error: one kind covers faults that are not the database's.
    assert "InternalClientError: cannot switch to state 11; another operation (2) is in progress" in caplog.text


async def test_a_closing_that_fails_after_its_caller_was_stopped_again_says_the_operation_stays_open(
    api, session_factory, redis_client, monkeypatch, caplog,
):
    issued, root = await bound_device(api)
    ops = DeviceOperations(session_factory, redis_client)
    request = request_for(UUID(issued["id"]), root)
    reached, let_go = asyncio.Event(), asyncio.Event()

    async def failing(self, *conditions, decided_for=None):
        reached.set()
        await let_go.wait()
        raise RuntimeError("the journal refused it")

    monkeypatch.setattr(DeviceOperations, "_cancel_where", failing)
    closing = asyncio.create_task(ops._cancel_own(request))
    await asyncio.wait_for(reached.wait(), 5.0)
    with caplog.at_level(logging.WARNING, logger="surogates.devices.operations"):
        # Its caller is stopped again: the closing goes on without it, and nobody awaits what it raises.
        await stop(closing)
        let_go.set()
        await eventually(lambda: said_it(caplog))
    assert caplog.text.count("it stays open") == 1 and "the journal refused it" in caplog.text


async def said_it(caplog) -> bool:
    return "it stays open" in caplog.text


async def test_a_database_error_that_is_not_a_blip_is_not_retried(
    api, session_factory, redis_client, monkeypatch,
):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])

    async def broken(self, operation_id):
        raise ProgrammingError("SELECT ...", {}, Exception("column does not exist"))

    monkeypatch.setattr(DeviceOperations, "_outcome", broken)
    ops = DeviceOperations(session_factory, redis_client, recheck_interval_s=0.2)
    with pytest.raises(ProgrammingError):
        await asyncio.wait_for(ops.run(request_for(device_id, root)), 2.0)


class StalledRedis:
    """The worker's Redis with one call that never answers, as a blackholed connection does.

    The worker's client sets no socket timeout, so nothing else ends the call.
    """

    def __init__(self, stalled: str) -> None:
        self.stalled = stalled

    def pubsub(self):
        return self

    async def publish(self, channel, message):
        if self.stalled == "publish":
            await hang()
        return 0

    async def subscribe(self, *channels):
        if self.stalled == "subscribe":
            await hang()
        if self.stalled == "close":
            raise RedisConnectionError("redis went away")

    async def get_message(self, **options):
        if self.stalled == "get_message":
            await hang()
        await asyncio.sleep(options["timeout"])

    async def aclose(self):
        if self.stalled == "close":
            await hang()


@pytest.mark.parametrize("stalled", ["publish", "subscribe", "get_message", "close"])
async def test_a_stalled_redis_does_not_stop_the_recheck(
    api, session_factory, redis_client, tmp_path, stalled,
):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    worker = DeviceOperations(session_factory, StalledRedis(stalled), recheck_interval_s=0.2)
    waiting = asyncio.create_task(worker.run(request_for(device_id, root)))
    await eventually(lambda: has_pending(worker, device_id))
    # Long enough for the worker to be inside the stalled call.
    await asyncio.sleep(0.6)
    api_side = DeviceOperations(session_factory, redis_client)
    await complete_pending(api_side, device_id, LocalWorkspaceIO(str(tmp_path)))
    assert await asyncio.wait_for(waiting, 3.0) == {"ok": True}


async def test_completing_checks_device_digest_and_credentials(api, session_factory, redis_client):
    issued, root = await bound_device(api)
    first = UUID(issued["id"])
    second = UUID((await register(api, name="Other laptop"))["id"])
    ops = DeviceOperations(session_factory, redis_client)
    waiting = asyncio.create_task(ops.run(request_for(first, root)))
    await eventually(lambda: has_pending(ops, first))
    [op] = await ops.pending(first, 1)
    assert await ops.pending(second, 1) == []

    assert await ops.complete(second, 1, op.id, op.digest, {"ok": True}) == "rejected"
    assert await ops.complete(first, 1, op.id, "0" * 64, {"ok": True}) == "rejected"
    # One the journal no longer has, as a request reaped since: nothing to record, nothing to refuse.
    assert await ops.complete(first, 1, uuid.uuid4(), op.digest, {"ok": True}) == "gone"
    assert await ops.complete(first, 2, op.id, op.digest, {"ok": True}) == "stale"
    assert await ops.complete(first, 1, op.id, op.digest, {"ok": True}) == "completed"
    assert await ops.complete(first, 1, op.id, op.digest, {"ok": True}) == "duplicate"
    assert await asyncio.wait_for(waiting, 2.0) == {"ok": True}


async def test_old_credentials_are_offered_no_operations(api, session_factory, redis_client):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client)
    waiting = asyncio.create_task(ops.run(request_for(device_id, root)))
    await eventually(lambda: has_pending(ops, device_id))
    await DeviceStore(session_factory).reauthorize(
        device_id, org_id=api.org_id, agent_id=AGENT_ID, user_id=api.user_id,
    )
    assert await ops.pending(device_id, 1) == []
    [op] = await ops.pending(device_id, 2)
    assert await ops.complete(device_id, 1, op.id, op.digest, {"ok": True}) == "stale"
    await stop(waiting)


async def test_pending_skips_operations_already_delivered(api, session_factory, redis_client):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client)
    waits = [asyncio.create_task(ops.run(request_for(device_id, root))) for _ in range(3)]

    async def three() -> bool:
        return len(await ops.pending(device_id, 1)) == 3

    await eventually(three)
    first, second, third = await ops.pending(device_id, 1)
    assert await ops.pending(device_id, 1, exclude={first.id, second.id}) == [third]
    assert [op.id for op in await ops.pending(device_id, 1, limit=1, exclude={first.id})] == [second.id]
    for task in waits:
        await stop(task)


async def test_revoking_fails_the_devices_open_operations(api, session_factory, redis_client):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client, recheck_interval_s=0.2)
    waiting = asyncio.create_task(ops.run(request_for(device_id, root)))
    await eventually(lambda: has_pending(ops, device_id))
    await api.client.delete(f"/v1/devices/{issued['id']}", headers=api.auth())
    assert await asyncio.wait_for(waiting, 2.0) == {"error": {
        "type": "revoked", "message": "Local access to this computer was revoked",
    }}
    # Restoring the device does not bring the cancelled work back.
    await api.client.post(f"/v1/devices/{issued['id']}/reauthorize", headers=api.auth())
    assert await ops.pending(device_id, 2) == []


async def test_a_nul_in_an_operation_is_kept(api, session_factory, redis_client):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client)
    waiting = asyncio.create_task(ops.run(request_for(device_id, root, args={"name": "a\x00b"})))
    await eventually(lambda: has_pending(ops, device_id))
    [op] = await ops.pending(device_id, 1)
    assert op.args == {"name": "a\x00b"}
    assert await ops.complete(device_id, 1, op.id, op.digest, {"ok": "x\x00y"}) == "completed"
    assert await asyncio.wait_for(waiting, 2.0) == {"ok": "x\x00y"}


async def test_a_completion_is_announced_on_the_operation_channel(api, session_factory, redis_client):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client)
    waiting = asyncio.create_task(ops.run(request_for(device_id, root)))
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


async def test_an_operation_that_is_not_valid_unicode_is_refused_and_records_nothing(
    api, session_factory, redis_client,
):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client)
    request = request_for(device_id, root, args={"name": "sh\ud83d"})
    with pytest.raises(ValueError, match="valid Unicode"):
        await asyncio.wait_for(ops.run(request), 2.0)
    assert await operation_rows(session_factory, device_id) == []


async def test_a_tool_calls_operations_are_numbered_in_order(api, session_factory, redis_client, tmp_path):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client)
    folder = LocalWorkspaceIO(str(tmp_path.resolve()))
    invocation = f"call-{uuid.uuid4()}"
    wio = DeviceWorkspaceIO(
        JournalRunner(
            ops, device_id=device_id, root_session_id=root, calling_session_id=root,
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
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client, recheck_interval_s=0.2)
    waiting = asyncio.create_task(ops.run(request_for(device_id, root)))
    await eventually(lambda: has_pending(ops, device_id))
    async with session_factory() as db:
        await purge_user_account(db, org_id=api.org_id, user_id=api.user_id)
        await db.commit()
    with pytest.raises(DeviceOperationError):
        await asyncio.wait_for(waiting, 2.0)


async def test_an_operation_for_a_revoked_device_fails_at_once(api, session_factory, redis_client):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    await api.client.delete(f"/v1/devices/{issued['id']}", headers=api.auth())
    ops = DeviceOperations(session_factory, redis_client, recheck_interval_s=0.2)
    assert await asyncio.wait_for(ops.run(request_for(device_id, root)), 2.0) == {"error": {
        "type": "revoked", "message": "Local access to this computer was revoked",
    }}
    assert await ops.pending(device_id, 1) == []


async def operation_rows(session_factory, device_id: UUID) -> list:
    async with session_factory() as db:
        return (await db.execute(
            select(DeviceOperation.id).where(
                DeviceOperation.device_id == device_id, DeviceOperation.kind != BIND,
            )
        )).all()


async def test_an_operation_for_a_removed_device_is_an_error_and_records_nothing(
    api, session_factory, redis_client,
):
    ops = DeviceOperations(session_factory, redis_client)
    with pytest.raises(DeviceOperationError, match="removed"):
        await asyncio.wait_for(ops.run(request_for(uuid.uuid4(), uuid.uuid4())), 2.0)


async def test_an_operation_for_a_revoked_device_is_recorded_as_refused(api, session_factory, redis_client):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    await api.client.delete(f"/v1/devices/{issued['id']}", headers=api.auth())
    ops = DeviceOperations(session_factory, redis_client)
    request = request_for(device_id, root)
    assert await asyncio.wait_for(ops.run(request), 2.0) == REVOKED_OUTCOME
    async with session_factory() as db:
        rows = (await db.execute(
            select(DeviceOperation.outcome, DeviceOperation.completed_at)
            .where(DeviceOperation.invocation_id == request.invocation_id)
        )).all()
    assert [(row.outcome, row.completed_at is not None) for row in rows] == [(REVOKED_OUTCOME, True)]


async def test_recording_waits_for_a_revocation_in_flight(api, session_factory, redis_client):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client, recheck_interval_s=0.2)
    async with session_factory() as db:
        # A revocation that has cancelled the device's open operations and not yet committed.
        await db.execute(update(Device).where(Device.id == device_id).values(revoked_at=func.now()))
        await db.execute(
            update(DeviceOperation)
            .where(DeviceOperation.device_id == device_id, DeviceOperation.completed_at.is_(None))
            .values(outcome=REVOKED_OUTCOME, completed_at=func.now())
        )
        request = request_for(device_id, root)
        recording = asyncio.create_task(ops.run(request))
        await asyncio.sleep(0.5)
        # An insert that committed now would be missed by the update above, and
        # would run after the device was reauthorized.
        assert await operation_rows(session_factory, device_id) == []
        await db.commit()
    assert await asyncio.wait_for(recording, 3.0) == REVOKED_OUTCOME
    async with session_factory() as db:
        rows = (await db.execute(
            select(DeviceOperation.outcome, DeviceOperation.completed_at)
            .where(DeviceOperation.invocation_id == request.invocation_id)
        )).all()
    assert [(row.outcome, row.completed_at is not None) for row in rows] == [(REVOKED_OUTCOME, True)]


async def test_a_reply_that_races_a_reauthorization_is_stale(
    api, session_factory, redis_client, monkeypatch,
):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client)
    waiting = asyncio.create_task(ops.run(request_for(device_id, root)))
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
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client)
    request = request_for(device_id, root)
    # The worker is gone before the device is revoked.
    await detached_run(SimpleNamespace(ops=ops, device_id=device_id), lambda: ops.run(request))
    await api.client.delete(f"/v1/devices/{issued['id']}", headers=api.auth())
    await api.client.post(f"/v1/devices/{issued['id']}/reauthorize", headers=api.auth())
    assert await ops.pending(device_id, 2) == []
    # The same tool call again gets the cancellation back and queues nothing.
    assert await asyncio.wait_for(ops.run(request), 1.0) == REVOKED_OUTCOME
    assert await ops.pending(device_id, 2) == []


async def test_a_repeated_request_on_a_revoked_device_gets_its_recorded_outcome(
    api, session_factory, redis_client, tmp_path,
):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client)
    request = request_for(device_id, root)
    waiting = asyncio.create_task(ops.run(request))
    await eventually(lambda: has_pending(ops, device_id))
    assert await complete_pending(ops, device_id, LocalWorkspaceIO(str(tmp_path))) == 1
    assert await asyncio.wait_for(waiting, 2.0) == {"ok": True}
    await api.client.delete(f"/v1/devices/{issued['id']}", headers=api.auth())
    # It ran before the revocation: a worker replaying the tool call must learn that.
    assert await asyncio.wait_for(ops.run(request), 1.0) == {"ok": True}


async def test_a_request_refused_for_revocation_stays_refused_after_reauthorization(
    api, session_factory, redis_client,
):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    await api.client.delete(f"/v1/devices/{issued['id']}", headers=api.auth())
    ops = DeviceOperations(session_factory, redis_client)
    request = request_for(device_id, root)
    assert await asyncio.wait_for(ops.run(request), 2.0) == REVOKED_OUTCOME
    await api.client.post(f"/v1/devices/{issued['id']}/reauthorize", headers=api.auth())
    assert await asyncio.wait_for(ops.run(request), 1.0) == REVOKED_OUTCOME
    assert await ops.pending(device_id, 2) == []


async def test_an_operation_for_a_session_on_another_computer_is_refused(api, session_factory, redis_client):
    _, root = await bound_device(api)
    other = UUID((await register(api, name="Other laptop"))["id"])
    ops = DeviceOperations(session_factory, redis_client)
    with pytest.raises(DeviceOperationError, match="does not work on this computer"):
        await ops.run(request_for(other, root))
    assert await operation_rows(session_factory, other) == []


async def test_an_operation_for_a_cloud_session_is_refused(api, session_factory, redis_client):
    device_id = UUID((await register(api))["id"])
    cloud = await SessionStore(session_factory).create_session(
        user_id=api.user_id, org_id=api.org_id, agent_id=AGENT_ID, channel="web", config={},
    )
    with pytest.raises(DeviceOperationError, match="does not work on this computer"):
        await DeviceOperations(session_factory, redis_client).run(request_for(device_id, cloud.id))
    assert await operation_rows(session_factory, device_id) == []


async def test_an_operation_for_another_users_session_is_refused(api, session_factory, redis_client):
    device_id = UUID((await register(api))["id"])
    someone_else, _ = await add_user(session_factory, api.org_id)
    theirs = await device_session(api, device_id, user_id=someone_else)
    with pytest.raises(DeviceOperationError, match="does not work on this computer"):
        await DeviceOperations(session_factory, redis_client).run(request_for(device_id, theirs))
    assert await operation_rows(session_factory, device_id) == []


async def test_an_operation_before_the_binding_is_answered_is_refused(api, session_factory, redis_client):
    device_id = UUID((await register(api))["id"])
    root = await device_session(api, device_id)
    ops = DeviceOperations(session_factory, redis_client)
    await ops.bind(session_id=root, device_id=device_id, folder=FOLDER, nonce=NONCE)
    assert await binding(api, root) == Binding("pending")
    with pytest.raises(DeviceOperationError, match="not set up"):
        await ops.run(request_for(device_id, root))


async def test_a_refused_binding_fails_the_session(api, session_factory, redis_client):
    device_id = UUID((await register(api))["id"])
    root = await device_session(api, device_id)
    ops = DeviceOperations(session_factory, redis_client)
    await ops.bind(session_id=root, device_id=device_id, folder=FOLDER, nonce=NONCE)
    [bind] = await ops.pending(device_id, 1)
    refusal = {"error": {"type": "binding", "message": "This folder was not confirmed on this computer"}}
    assert await ops.complete(device_id, 1, bind.id, bind.digest, refusal) == "completed"
    assert await binding(api, root) == Binding("failed", "This folder was not confirmed on this computer")
    with pytest.raises(DeviceOperationError, match="not set up"):
        await ops.run(request_for(device_id, root))


async def test_a_session_whose_binding_was_never_recorded_has_failed(api):
    device_id = UUID((await register(api))["id"])
    root = await device_session(api, device_id)
    assert await binding(api, root) == Binding("failed", "The computer was never asked to set it up")


async def test_a_malformed_execution_config_names_no_device():
    for config in ({"execution": "device"}, {"execution": ["device"]}, {"execution": None}, None):
        assert device_of(config) is None


async def test_a_binding_on_a_revoked_device_fails(api, session_factory, redis_client):
    issued = await register(api)
    device_id = UUID(issued["id"])
    root = await device_session(api, device_id)
    await api.client.delete(f"/v1/devices/{issued['id']}", headers=api.auth())
    await DeviceOperations(session_factory, redis_client).bind(
        session_id=root, device_id=device_id, folder=FOLDER, nonce=NONCE,
    )
    assert await binding(api, root) == Binding("failed", "Local access to this computer was revoked")


async def test_revoking_fails_a_binding_in_progress(api, session_factory, redis_client):
    issued = await register(api)
    device_id = UUID(issued["id"])
    root = await device_session(api, device_id)
    ops = DeviceOperations(session_factory, redis_client)
    await ops.bind(session_id=root, device_id=device_id, folder=FOLDER, nonce=NONCE)
    await api.client.delete(f"/v1/devices/{issued['id']}", headers=api.auth())
    await api.client.post(f"/v1/devices/{issued['id']}/reauthorize", headers=api.auth())
    assert await binding(api, root) == Binding("failed", "Local access to this computer was revoked")
    assert await ops.pending(device_id, 2) == []


async def test_a_child_session_works_in_its_roots_folder(api, session_factory, redis_client, tmp_path):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    store = SessionStore(session_factory)
    child = await create_child_session(
        store=store, parent=await store.get_session(root), channel="delegation",
    )
    ops = DeviceOperations(session_factory, redis_client)
    request = OperationRequest(**{**_fields(request_for(device_id, root)), "calling_session_id": child.id})
    waiting = asyncio.create_task(ops.run(request))
    await eventually(lambda: has_pending(ops, device_id))
    assert await complete_pending(ops, device_id, LocalWorkspaceIO(str(tmp_path))) == 1
    assert await asyncio.wait_for(waiting, 2.0) == {"ok": True}


async def test_a_session_outside_the_root_cannot_use_its_folder(api, session_factory, redis_client):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    stranger = await device_session(api, device_id)
    request = OperationRequest(**{**_fields(request_for(device_id, root)), "calling_session_id": stranger})
    with pytest.raises(DeviceOperationError, match="does not work on this computer"):
        await DeviceOperations(session_factory, redis_client).run(request)


async def test_only_the_root_records_its_binding(api, session_factory, redis_client):
    issued, root = await bound_device(api)
    request = OperationRequest(**{**_fields(request_for(UUID(issued["id"]), root)), "kind": BIND})
    with pytest.raises(ValueError, match="binding"):
        await DeviceOperations(session_factory, redis_client).run(request)


async def test_an_ordinary_operation_cannot_take_the_bindings_place(api, session_factory, redis_client):
    device_id = UUID((await register(api))["id"])
    root = await device_session(api, device_id)
    slot = OperationRequest(**{
        **_fields(request_for(device_id, root)), "invocation_id": BIND, "ordinal": 0,
    })
    with pytest.raises(ValueError, match="binding"):
        await DeviceOperations(session_factory, redis_client).run(slot)
    assert await binding(api, root) == Binding("failed", "The computer was never asked to set it up")


async def test_only_a_bind_row_counts_as_the_binding(api, session_factory):
    device_id = UUID((await register(api))["id"])
    root = await device_session(api, device_id)
    async with session_factory() as db:
        # A row in the binding's slot that is not a binding, as a bug could write it.
        db.add(DeviceOperation(
            device_id=device_id, root_session_id=root, calling_session_id=root,
            invocation_id=BIND, ordinal=0, kind="which", args={"name": "sh"},
            digest="x", outcome={"ok": True}, completed_at=func.now(),
        ))
        await db.commit()
    assert (await binding(api, root)).state != "bound"


async def test_a_child_session_is_never_bound_on_its_own(api, session_factory, redis_client):
    issued, root = await bound_device(api)
    store = SessionStore(session_factory)
    child = await create_child_session(
        store=store, parent=await store.get_session(root), channel="delegation",
    )
    with pytest.raises(ValueError, match="root"):
        await DeviceOperations(session_factory, redis_client).bind(
            session_id=child.id, device_id=UUID(issued["id"]), folder=FOLDER, nonce=NONCE,
        )


def device_io(ops: DeviceOperations, device_id: UUID, root: UUID, folder: Path) -> DeviceWorkspaceIO:
    """A WorkspaceIO for one tool call on *folder*, through the journal."""
    return DeviceWorkspaceIO(
        JournalRunner(
            ops, device_id=device_id, root_session_id=root, calling_session_id=root,
            invocation_id=f"call-{uuid.uuid4()}",
        ),
        root=str(folder),
    )


@pytest_asyncio.fixture(loop_scope="session")
async def laptop_rig(api, link_url, session_factory, redis_client, tmp_path):
    """A registered device, its folder, a worker-side journal, and a disconnected fake laptop."""
    issued, root = await bound_device(api)
    folder = (tmp_path / "laptop").resolve()
    folder.mkdir()
    laptop = FakeLaptop(link_url, issued["token"], LocalWorkspaceIO(str(folder)))
    rig = SimpleNamespace(
        device_id=UUID(issued["id"]),
        root=root,
        token=issued["token"],
        folder=folder,
        ops=DeviceOperations(session_factory, redis_client),
        laptop=laptop,
        lease=await SessionStore(session_factory).try_acquire_lease(root, "test-worker", ttl_seconds=600),
    )
    yield rig
    await laptop.disconnect()


async def _first_op(ws) -> dict:
    while True:
        frame = await receive(ws)
        if frame["type"] == "op":
            return frame


_PAGE = {"encoding": "utf-8", "offset": 1, "limit": 10, "max_bytes": 100}
_STARTED = {"task_id": "nul", "pty": False, "notify_on_complete": False, "watcher_interval": None}
# Each operation that hands a text of the model's, or a key, to the computer, with a NUL in it.
WITH_A_NUL = {
    "resolve": lambda io: io.resolve("a\0b"),
    "check_write": lambda io: io.check_write("a\0b"),
    "read": lambda io: io.read("/f/a\0b"),
    "read_lines": lambda io: io.read_lines("/f/a\0b", **_PAGE),
    "write": lambda io: io.write("/f/a\0b", b"x"),
    "a large write": lambda io: io.write("/f/a\0b", b"x" * (workspace_module.MAX_PAYLOAD_BYTES + 1)),
    "delete": lambda io: io.delete("/f/a\0b"),
    "list_dir": lambda io: io.list_dir("/f/a\0b"),
    "walk": lambda io: io.walk("/f/a\0b", skip=()),
    "local_file": lambda io: io.local_file("/f/a\0b").__aenter__(),
    "ripgrep key": lambda io: io.ripgrep("/f/a\0b", mode="count", pattern="x"),
    "ripgrep pattern": lambda io: io.ripgrep("/f", mode="count", pattern="a\0"),
    "ripgrep glob": lambda io: io.ripgrep("/f", mode="count", pattern="x", glob="*\0"),
    "run command": lambda io: io.run("a\0b", workdir=None, timeout=10),
    "run workdir": lambda io: io.run("pwd", workdir="a\0b", timeout=10),
    "start command": lambda io: io.start("a\0b", workdir=None, **_STARTED),
    "start workdir": lambda io: io.start("true", workdir="a\0b", **_STARTED),
}


@pytest.mark.parametrize("call", WITH_A_NUL.values(), ids=WITH_A_NUL)
async def test_a_nul_is_refused_at_once_with_the_computer_away_and_nothing_is_asked_of_it(laptop_rig, session_factory, call):
    rig = laptop_rig
    wio = device_io(rig.ops, rig.device_id, rig.root, rig.folder)
    before = len(await operation_rows(session_factory, rig.device_id))
    # The computer is away: an operation recorded for it would wait until it is back, and
    # in a chat that asks every time its user would be asked about what can never run.
    with pytest.raises(ValueError) as refused:
        await asyncio.wait_for(call(wio), 3.0)
    assert str(refused.value) == NUL_REFUSED
    assert len(await operation_rows(session_factory, rig.device_id)) == before


async def test_a_stat_of_a_key_with_a_nul_finds_nothing_with_the_computer_away(laptop_rig, session_factory):
    rig = laptop_rig
    wio = device_io(rig.ops, rig.device_id, rig.root, rig.folder)
    before = len(await operation_rows(session_factory, rig.device_id))
    assert await asyncio.wait_for(wio.stat("/f/a\0b"), 3.0) is None
    assert len(await operation_rows(session_factory, rig.device_id)) == before


@pytest.mark.parametrize("arguments", [
    {"pattern": "a\0"},
    {"pattern": "x", "file_glob": "*\0"},
    {"pattern": "a\0", "path": "missing"},
    {"pattern": "*\0", "target": "files"},
    {"pattern": "x", "path": "a\0b"},
], ids=["pattern", "glob", "pattern, path missing", "file pattern", "path"])
async def test_a_search_with_a_nul_is_refused_at_once_with_the_computer_away(laptop_rig, session_factory, arguments):
    rig = laptop_rig
    wio = device_io(rig.ops, rig.device_id, rig.root, rig.folder)
    before = len(await operation_rows(session_factory, rig.device_id))
    # Before the path is resolved, which asks the computer: nothing waits for it.
    answer = await asyncio.wait_for(file_ops._search_files_handler(arguments, workspace_io=wio), 3.0)
    assert json.loads(answer) == {"error": f"Search failed: {NUL_REFUSED}"}
    assert len(await operation_rows(session_factory, rig.device_id)) == before


@pytest.mark.parametrize("action", ["poll", "log", "wait", "kill", "write", "submit"])
async def test_a_process_named_with_a_nul_is_unknown_at_once_with_the_computer_away(laptop_rig, session_factory, action):
    rig = laptop_rig
    wio = device_io(rig.ops, rig.device_id, rig.root, rig.folder)
    before = len(await operation_rows(session_factory, rig.device_id))
    # No process has such a name: said as the tool says it of any it does not know, without asking the computer.
    answer = await asyncio.wait_for(
        process_registry._handle_process({"action": action, "session_id": "proc_\0", "data": "x"}, workspace_io=wio), 3.0,
    )
    assert json.loads(answer) == {"status": "not_found", "error": "No process with ID proc_\0"}
    assert len(await operation_rows(session_factory, rig.device_id)) == before


async def test_a_nul_in_what_a_process_is_sent_or_a_browser_is_told_reaches_the_computer(laptop_rig):
    """Content, not a name: recorded as given while the computer is away, and delivered when it is back."""
    rig = laptop_rig
    heard: list[tuple[str, dict]] = []
    rig.laptop.browser = lambda kind, args: heard.append((kind, args)) or {"ok": {"value": None}}
    wio = device_io(rig.ops, rig.device_id, rig.root, rig.folder)
    client = DeviceBrowserClient(JournalRunner(
        rig.ops, device_id=rig.device_id, root_session_id=rig.root, calling_session_id=rig.root,
        invocation_id=f"call-{uuid.uuid4()}",
    ))
    calls = [
        asyncio.create_task(wio.write_stdin("proc_000000000000", "a\0b")),
        asyncio.create_task(client.evaluate("return 'a\0b'")),
        asyncio.create_task(client.type_text("a\0b")),
        asyncio.create_task(client.navigate("https://example.org/a\0b")),
    ]
    try:
        async def all_recorded() -> bool:
            return len(await rig.ops.pending(rig.device_id, 1)) == len(calls)

        await eventually(all_recorded)
        recorded = {op.kind: op.args for op in await rig.ops.pending(rig.device_id, 1)}
        assert recorded["write_stdin"] == {"session_id": "proc_000000000000", "data": "a\0b"}
        assert all("a\\u0000b" in json.dumps(args) for args in recorded.values()), recorded
        await rig.laptop.connect()
        # Every one is answered by the computer, whatever it answers.
        done, pending = await asyncio.wait(calls, timeout=10.0)
        assert not pending
        assert sorted(rig.laptop.ran) == sorted(recorded)
        assert all("a\\u0000b" in json.dumps(args) for _, args in heard) and len(heard) == 3
    finally:
        for call in calls:
            call.cancel()
        await asyncio.gather(*calls, return_exceptions=True)


async def test_operations_run_on_the_laptop(laptop_rig):
    rig = laptop_rig
    await rig.laptop.connect()
    wio = device_io(rig.ops, rig.device_id, rig.root, rig.folder)
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
    wio = device_io(rig.ops, rig.device_id, rig.root, rig.folder)
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
    wio = device_io(rig.ops, rig.device_id, rig.root, rig.folder)
    nul = await asyncio.wait_for(wio.run("printf 'a\\000b'", workdir=None, timeout=10), 5.0)
    assert nul.output == "a\x00b"
    big = await asyncio.wait_for(
        wio.run("head -c 3000000 /dev/zero | tr '\\000' x", workdir=None, timeout=30), 10.0,
    )
    assert "chars omitted by the computer" in big.output
    assert rig.laptop.connected


async def test_a_screenshot_over_a_mebibyte_comes_back_whole_from_the_computer(laptop_rig):
    rig = laptop_rig
    png = b"\x89PNG\r\n\x1a\n" + os.urandom(1536 * 1024)
    rig.laptop.browser = lambda kind, args: {"ok": base64.b64encode(png).decode("ascii")}
    await rig.laptop.connect()
    client = DeviceBrowserClient(JournalRunner(
        rig.ops, device_id=rig.device_id, root_session_id=rig.root, calling_session_id=rig.root,
        invocation_id=f"call-{uuid.uuid4()}",
    ))

    shot = await asyncio.wait_for(client.screenshot(), 10.0)

    assert shot["png_bytes"] == png
    assert rig.laptop.ran == ["browser.screenshot"]
    assert rig.laptop.chunks_sent


async def test_a_scripts_value_shaped_as_a_transfer_comes_back_as_its_value_with_the_link_up(laptop_rig):
    rig = laptop_rig
    returned = {"transfer": {"size": 5, "sha256": "a" * 64}}
    rig.laptop.browser = lambda kind, args: {"ok": {"value": returned}}
    await rig.laptop.connect()
    client = DeviceBrowserClient(JournalRunner(
        rig.ops, device_id=rig.device_id, root_session_id=rig.root, calling_session_id=rig.root,
        invocation_id=f"call-{uuid.uuid4()}",
    ))

    assert await asyncio.wait_for(client.evaluate("return await (await fetch('/api/transfers/1')).json();"), 10.0) == returned
    # Never read as the link's own framing: the computer stays connected.
    assert rig.laptop.connected
    assert rig.laptop.ran == ["browser.evaluate"]


async def test_a_tool_handler_works_over_the_link(laptop_rig):
    rig = laptop_rig
    await rig.laptop.connect()
    written = json.loads(await asyncio.wait_for(file_ops._write_file_handler(
        {"path": "plan.md", "content": "# Plan\n"},
        workspace_io=device_io(rig.ops, rig.device_id, rig.root, rig.folder),
    ), 5.0))
    assert written["status"] == "ok", written
    assert (rig.folder / "plan.md").read_text() == "# Plan\n"
    read = json.loads(await asyncio.wait_for(file_ops._read_file_handler(
        {"path": "plan.md"}, workspace_io=device_io(rig.ops, rig.device_id, rig.root, rig.folder),
    ), 5.0))
    assert read["content"] == "# Plan\n"


async def test_the_app_binds_a_session_over_the_link(laptop_rig, api):
    rig = laptop_rig
    rig.laptop.prepare(NONCE, FOLDER)
    await rig.laptop.connect()
    root = await device_session(api, rig.device_id)
    await rig.ops.bind(session_id=root, device_id=rig.device_id, folder=FOLDER, nonce=NONCE)
    await eventually(binding_is(api, root, "bound"))
    assert rig.laptop.bindings == {str(root): FOLDER}


async def test_the_app_refuses_a_folder_its_user_did_not_confirm(laptop_rig, api):
    rig = laptop_rig
    rig.laptop.prepare(NONCE, "/home/flavius/elsewhere")
    await rig.laptop.connect()
    root = await device_session(api, rig.device_id)
    await rig.ops.bind(session_id=root, device_id=rig.device_id, folder=FOLDER, nonce=NONCE)
    await eventually(binding_is(api, root, "failed"))
    assert await binding(api, root) == Binding("failed", "This folder was not confirmed on this computer")
    assert rig.laptop.bindings == {}


def builtin_tools() -> ToolRegistry:
    registry = ToolRegistry()
    ToolRuntime(registry).register_builtins()
    return registry


async def tool_call(
    rig, store, tools, call_id: str, name: str, args: dict, *, redis_client, session_factory, lease=None,
) -> dict:
    """One tool call of the rig's bound session, as the harness makes it."""
    return await asyncio.wait_for(execute_single_tool(
        {"id": call_id, "function": {"name": name, "arguments": json.dumps(args)}},
        session=await store.get_session(rig.root),
        lease=lease or rig.lease,
        store=store,
        tools=tools,
        tenant=MagicMock(asset_root="/tmp/test"),
        redis=redis_client,
        session_factory=session_factory,
    ), 15.0)


async def test_an_agent_edits_a_file_on_the_computer(laptop_rig, session_factory, redis_client):
    rig = laptop_rig
    await rig.laptop.connect()
    store, tools = SessionStore(session_factory), builtin_tools()
    io = {"redis_client": redis_client, "session_factory": session_factory}
    await tool_call(rig, store, tools, "call_1", "write_file", {"path": "notes.md", "content": "from the agent\n"}, **io)
    assert (rig.folder / "notes.md").read_text() == "from the agent\n"
    read = await tool_call(rig, store, tools, "call_2", "read_file", {"path": "notes.md"}, **io)
    assert "from the agent" in read["content"]


async def test_a_tool_call_is_journaled_under_its_own_event(laptop_rig, session_factory, redis_client):
    rig = laptop_rig
    await rig.laptop.connect()
    store = SessionStore(session_factory)
    await tool_call(
        rig, store, builtin_tools(), "call_1", "write_file", {"path": "a.md", "content": "a"},
        redis_client=redis_client, session_factory=session_factory,
    )
    async with session_factory() as db:
        call_event = (await db.execute(
            select(Event.id)
            .where(Event.session_id == rig.root, Event.type == EventType.TOOL_CALL.value)
            .order_by(Event.id.desc())
            .limit(1)
        )).scalar_one()
        invocations = set((await db.execute(
            select(DeviceOperation.invocation_id)
            .where(DeviceOperation.root_session_id == rig.root, DeviceOperation.kind != BIND)
        )).scalars())
    assert invocations == {f"{call_event}:call_1"}


async def take_over(store: SessionStore, rig):
    """Another worker takes the session: the rig's lease is released and a new one taken."""
    await store.release_lease(rig.root, rig.lease.lease_token)
    rig.lease = await store.try_acquire_lease(rig.root, "worker-b", ttl_seconds=600)
    return rig.lease


async def test_a_worker_that_lost_the_session_commits_no_result(laptop_rig, session_factory, redis_client):
    rig = laptop_rig
    await rig.laptop.connect()
    store, tools = SessionStore(session_factory), builtin_tools()
    stale = rig.lease
    await take_over(store, rig)
    with pytest.raises(asyncio.CancelledError):
        await tool_call(
            rig, store, tools, "call_1", "write_file", {"path": "a.md", "content": "a"},
            redis_client=redis_client, session_factory=session_factory, lease=stale,
        )
    assert await store.get_events(rig.root, types=[EventType.TOOL_RESULT]) == []
    assert not (rig.folder / "a.md").exists()


async def test_a_dispatched_turn_that_lost_the_session_stops(laptop_rig, session_factory, redis_client):
    rig = laptop_rig
    await rig.laptop.connect()
    store, tools = SessionStore(session_factory), builtin_tools()
    stale = rig.lease
    await take_over(store, rig)
    turn: list[TurnSlots] = []

    async def run_turn() -> None:
        slots = TurnSlots(
            semaphore=asyncio.Semaphore(1), gate=None, org_id="", agent_id="",
            gate_held=False, task=asyncio.current_task(),
        )
        turn.append(slots)
        current_turn.set(slots)
        async with slots.activity():
            await tool_call(
                rig, store, tools, "call_1", "write_file", {"path": "a.md", "content": "a"},
                redis_client=redis_client, session_factory=session_factory, lease=stale,
            )

    task = asyncio.create_task(run_turn())
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 15.0)
    assert task.cancelled() and turn[0].detached
    assert await store.get_events(rig.root, types=[EventType.TOOL_RESULT]) == []


async def call_event_of(store: SessionStore, session_id: UUID, call_id: str) -> int:
    [event] = [
        e for e in await store.get_events(session_id, types=[EventType.TOOL_CALL])
        if e.data.get("tool_call_id") == call_id
    ]
    return event.id


async def forget_result(store: SessionStore, session_factory, session_id: UUID, call_id: str) -> None:
    """The worker stopped before committing the call's result: the journal has it, the event log does not."""
    ids = [
        e.id for e in await store.get_events(session_id, types=[EventType.TOOL_RESULT])
        if e.data.get("tool_call_id") == call_id
    ]
    async with session_factory() as db:
        await db.execute(delete(Event).where(Event.id.in_(ids)))
        await db.commit()


async def resume_call(
    rig, store, tools, call_id: str, name: str, args: dict, *, redis_client, session_factory, governance_gate=None,
) -> dict:
    """The call run again by a worker resuming it: under its first tool.call event."""
    return await asyncio.wait_for(execute_single_tool(
        {
            "id": call_id,
            "function": {"name": name, "arguments": json.dumps(args)},
        },
        session=await store.get_session(rig.root),
        lease=rig.lease,
        store=store,
        tools=tools,
        tenant=MagicMock(asset_root="/tmp/test"),
        redis=redis_client,
        session_factory=session_factory,
        governance_gate=governance_gate,
        replay_of=await call_event_of(store, rig.root, call_id),
    ), 15.0)


async def test_a_resumed_call_gets_what_the_computer_already_did_without_running_it_again(
    laptop_rig, session_factory, redis_client,
):
    rig = laptop_rig
    await rig.laptop.connect()
    store, tools = SessionStore(session_factory), builtin_tools()
    io = {"redis_client": redis_client, "session_factory": session_factory}
    args = {"command": "echo once >> log.txt"}
    first = await tool_call(rig, store, tools, "call_1", "terminal", args, **io)
    await forget_result(store, session_factory, rig.root, "call_1")
    ran = len(rig.laptop.ran)
    await take_over(store, rig)

    resumed = await resume_call(rig, store, tools, "call_1", "terminal", args, **io)

    assert (rig.folder / "log.txt").read_text() == "once\n"
    assert len(rig.laptop.ran) == ran
    assert json.loads(resumed["content"])["exit_code"] == json.loads(first["content"])["exit_code"] == 0
    assert len(await store.get_events(rig.root, types=[EventType.TOOL_CALL])) == 1
    assert len(await store.get_events(rig.root, types=[EventType.TOOL_RESULT])) == 1


async def test_a_call_dict_cannot_mark_itself_resumed(laptop_rig, session_factory, redis_client):
    rig = laptop_rig
    await rig.laptop.connect()
    store, tools = SessionStore(session_factory), builtin_tools()
    # A model provider can put any field on a tool call it sends.
    call = {
        "id": "call_1",
        "function": {"name": "terminal", "arguments": json.dumps({"command": "echo once >> log.txt"})},
        "_replay_of": 1,
    }

    refused = await asyncio.wait_for(execute_single_tool(
        call,
        session=await store.get_session(rig.root),
        lease=rig.lease,
        store=store,
        tools=tools,
        tenant=MagicMock(asset_root="/tmp/test"),
        redis=redis_client,
        session_factory=session_factory,
        governance_gate=GovernanceGate(require_approval={"terminal"}),
    ), 15.0)

    assert json.loads(refused["content"])["error"] == "policy_blocked_overridable"
    assert not (rig.folder / "log.txt").exists()
    assert len(await store.get_events(rig.root, types=[EventType.POLICY_DENIED])) == 1
    assert len(await store.get_events(rig.root, types=[EventType.TOOL_CALL])) == 1


async def test_a_resumed_call_is_not_asked_for_an_approval_its_first_run_spent(
    laptop_rig, session_factory, redis_client,
):
    rig = laptop_rig
    await rig.laptop.connect()
    store, tools = SessionStore(session_factory), builtin_tools()
    io = {"redis_client": redis_client, "session_factory": session_factory}
    args = {"command": "echo once >> log.txt"}
    # The call ran once, approved, before its worker stopped.
    await tool_call(rig, store, tools, "call_1", "terminal", args, **io)
    await forget_result(store, session_factory, rig.root, "call_1")
    await take_over(store, rig)

    resumed = await resume_call(
        rig, store, tools, "call_1", "terminal", args,
        governance_gate=GovernanceGate(require_approval={"terminal"}), **io,
    )

    assert json.loads(resumed["content"])["exit_code"] == 0
    assert (rig.folder / "log.txt").read_text() == "once\n"
    assert await store.get_events(rig.root, types=[EventType.INBOX_GOVERNANCE_GATE, EventType.POLICY_DENIED]) == []


async def test_a_resumed_call_that_asks_for_something_else_is_reported_interrupted(
    laptop_rig, session_factory, redis_client,
):
    rig = laptop_rig
    await rig.laptop.connect()
    store, tools = SessionStore(session_factory), builtin_tools()
    io = {"redis_client": redis_client, "session_factory": session_factory}
    await tool_call(rig, store, tools, "call_1", "write_file", {"path": "a.md", "content": "first"}, **io)
    await forget_result(store, session_factory, rig.root, "call_1")
    await take_over(store, rig)

    resumed = await resume_call(rig, store, tools, "call_1", "write_file", {"path": "a.md", "content": "second"}, **io)

    assert resumed["content"] == INTERRUPTED
    assert (rig.folder / "a.md").read_text() == "first"
    [result] = await store.get_events(rig.root, types=[EventType.TOOL_RESULT])
    assert result.data["content"] == INTERRUPTED


async def test_a_resumed_call_that_took_another_path_asks_the_computer_for_nothing_more(
    laptop_rig, session_factory, redis_client,
):
    rig = laptop_rig
    await rig.laptop.connect()

    def runner() -> JournalRunner:
        return JournalRunner(
            rig.ops, device_id=rig.device_id, root_session_id=rig.root, calling_session_id=rig.root,
            invocation_id="call-event:call_1", lease_token=str(rig.lease.lease_token),
        )

    def command(text: str) -> dict:
        return {"command": f"echo {text} >> log.txt", "workdir": None, "timeout": 10}

    await asyncio.wait_for(runner().run("run", command("first")), 10.0)
    resumed = runner()
    with pytest.raises(OperationConflict):
        await resumed.run("run", command("changed"))
    with pytest.raises(OperationConflict):
        await resumed.run("run", command("after"))
    assert await resumed.diverged()
    assert (rig.folder / "log.txt").read_text() == "first\n"


async def test_a_resumed_call_that_stops_short_is_reported_interrupted(laptop_rig, session_factory, redis_client):
    rig = laptop_rig
    await rig.laptop.connect()
    store, tools = SessionStore(session_factory), builtin_tools()
    io = {"redis_client": redis_client, "session_factory": session_factory}
    (rig.folder / "notes.md").write_text("old\n")
    await tool_call(rig, store, tools, "call_1", "read_file", {"path": "notes.md"}, **io)
    await tool_call(rig, store, tools, "call_2", "write_file", {"path": "notes.md", "content": "new\n"}, **io)
    await forget_result(store, session_factory, rig.root, "call_2")
    # A new worker has not seen the read, so its write_file refuses to overwrite.
    file_ops._read_tracker.clear()
    await take_over(store, rig)

    resumed = await resume_call(rig, store, tools, "call_2", "write_file", {"path": "notes.md", "content": "new\n"}, **io)

    # The write did happen: "refusing to overwrite" would tell the model it did not.
    assert resumed["content"] == INTERRUPTED
    assert (rig.folder / "notes.md").read_text() == "new\n"


async def test_a_resumed_harness_tool_is_reported_interrupted_without_running_again(
    laptop_rig, session_factory, redis_client, monkeypatch,
):
    rig = laptop_rig
    await rig.laptop.connect()
    store, tools = SessionStore(session_factory), builtin_tools()
    io = {"redis_client": redis_client, "session_factory": session_factory}
    ran: list[dict] = []

    async def handler(arguments: dict, **kwargs) -> str:
        # Whatever else it does off the computer is done again by running it again.
        ran.append(arguments)
        wio = kwargs["workspace_io"]
        await wio.write(await wio.resolve("made.md"), b"made")
        return json.dumps({"ok": True})

    tools.register(
        "harness_probe",
        ToolSchema(name="harness_probe", description="probe", parameters={"type": "object", "properties": {}}),
        handler=handler,
    )
    monkeypatch.setitem(TOOL_LOCATIONS, "harness_probe", ToolLocation.HARNESS)
    first = await tool_call(rig, store, tools, "call_1", "harness_probe", {}, **io)
    assert json.loads(first["content"]) == {"ok": True}
    await forget_result(store, session_factory, rig.root, "call_1")
    await take_over(store, rig)

    resumed = await resume_call(rig, store, tools, "call_1", "harness_probe", {}, **io)

    assert resumed["content"] == INTERRUPTED
    assert len(ran) == 1
    [result] = await store.get_events(rig.root, types=[EventType.TOOL_RESULT])
    assert result.data["content"] == INTERRUPTED


async def test_a_resumed_browser_call_is_reported_interrupted_without_acting_again(
    laptop_rig, session_factory, redis_client,
):
    rig = laptop_rig
    acted: list[str] = []

    def browse(kind: str, args: dict) -> dict:
        acted.append(kind)
        return {"ok": {}}

    rig.laptop.browser = browse
    await rig.laptop.connect()
    store, tools = SessionStore(session_factory), builtin_tools()
    io = {"redis_client": redis_client, "session_factory": session_factory}
    first = await tool_call(rig, store, tools, "call_1", "browser_click", {"x": 5, "y": 6}, **io)
    assert json.loads(first["content"]) == {"clicked": True}
    await forget_result(store, session_factory, rig.root, "call_1")
    await take_over(store, rig)

    resumed = await resume_call(rig, store, tools, "call_1", "browser_click", {"x": 5, "y": 6}, **io)

    # A click on the user's own browser is not clicked again: what it did is for the user to see,
    # and for the agent to read on the page, not in the folder.
    assert resumed["content"] == INTERRUPTED_IN_BROWSER
    assert "browser_get_state" in resumed["content"] and "folder" not in resumed["content"]
    assert acted == ["browser.mouse"]


async def test_a_screenshot_over_a_mebibyte_crosses_the_link_and_lands_in_the_folder(
    laptop_rig, session_factory, redis_client,
):
    rig = laptop_rig
    png = b"\x89PNG\r\n\x1a\n" + os.urandom(1536 * 1024)
    rig.laptop.browser = lambda kind, args: {"ok": base64.b64encode(png).decode("ascii")}
    await rig.laptop.connect()
    store, tools = SessionStore(session_factory), builtin_tools()

    result = await tool_call(
        rig, store, tools, "call_1", "browser_screenshot", {},
        redis_client=redis_client, session_factory=session_factory,
    )

    body = json.loads(result["content"])
    assert (rig.folder / body["relative_path"]).read_bytes() == png
    # Both ways in chunks: the shot from the computer, then the file back to it.
    assert rig.laptop.chunks_sent and rig.laptop.chunks_received


async def test_a_resumed_patch_after_a_read_returns_what_the_computer_did(
    laptop_rig, session_factory, redis_client,
):
    rig = laptop_rig
    await rig.laptop.connect()
    store, tools = SessionStore(session_factory), builtin_tools()
    io = {"redis_client": redis_client, "session_factory": session_factory}
    (rig.folder / "notes.md").write_text("one\ntwo\nthree\n")
    patch = {"path": "notes.md", "old_string": "two", "new_string": "2"}
    await tool_call(rig, store, tools, "call_1", "read_file", {"path": "notes.md"}, **io)
    first = await tool_call(rig, store, tools, "call_2", "patch", patch, **io)
    await forget_result(store, session_factory, rig.root, "call_2")
    # A new worker has not seen the read; the patch asks the computer for the same operations all the same.
    file_ops._read_tracker.clear()
    await take_over(store, rig)

    resumed = await resume_call(rig, store, tools, "call_2", "patch", patch, **io)

    assert resumed["content"] != INTERRUPTED
    assert resumed["content"] == first["content"]
    assert json.loads(resumed["content"]).get("error") is None
    assert (rig.folder / "notes.md").read_text() == "one\n2\nthree\n"
    # The write expects the revision of the stat before its read. The resumed call got that stat's recorded
    # outcome back, not today's file, so it asked for the same write.
    async with session_factory() as db:
        rows = (await db.execute(
            select(DeviceOperation.kind, DeviceOperation.args, DeviceOperation.outcome)
            .where(DeviceOperation.root_session_id == rig.root, DeviceOperation.invocation_id.endswith(":call_2"))
            .order_by(DeviceOperation.ordinal)
        )).all()
    kinds = [row.kind for row in rows]
    stat, write = rows[kinds.index("read") - 1], rows[kinds.index("write")]
    assert stat.kind == "stat"
    assert write.args["expected_revision"] == stat.outcome["ok"]["revision"]
    now = await LocalWorkspaceIO(str(rig.folder)).stat(str(rig.folder / "notes.md"))
    assert write.args["expected_revision"] != now.revision


def model_call(call_id: str, name: str, args: dict) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


async def resume_session(rig, store, tools, messages: list[dict], *, redis_client, session_factory) -> None:
    """Resume the session's unanswered calls as a new worker's wake does."""
    session = await store.get_session(rig.root)

    async def run_tool(call: dict, event_id: int) -> dict:
        return await asyncio.wait_for(execute_single_tool(
            call, replay_of=event_id, session=session, lease=rig.lease, store=store, tools=tools,
            tenant=MagicMock(asset_root="/tmp/test"), redis=redis_client, session_factory=session_factory,
        ), 15.0)

    await replay_unanswered(
        session=session, events=await store.get_events(rig.root), messages=messages,
        session_factory=session_factory, run_tool=run_tool,
    )


async def test_a_session_whose_worker_stopped_after_the_computer_answered_is_resumed_without_running_it_again(
    laptop_rig, session_factory, redis_client,
):
    rig = laptop_rig
    await rig.laptop.connect()
    store, tools = SessionStore(session_factory), builtin_tools()
    io = {"redis_client": redis_client, "session_factory": session_factory}
    args = {"command": "echo once >> log.txt"}
    assistant = {"role": "assistant", "content": "", "tool_calls": [model_call("call_1", "terminal", args)]}
    await store.emit_event(rig.root, EventType.LLM_RESPONSE, {"message": assistant})
    await tool_call(rig, store, tools, "call_1", "terminal", args, **io)
    await forget_result(store, session_factory, rig.root, "call_1")
    ran = len(rig.laptop.ran)
    await take_over(store, rig)
    messages = [{"role": "user", "content": "log it"}, assistant]

    await resume_session(rig, store, tools, messages, **io)

    assert (rig.folder / "log.txt").read_text() == "once\n"
    assert len(rig.laptop.ran) == ran
    assert messages[2]["role"] == "tool" and messages[2]["tool_call_id"] == "call_1"
    assert len(await store.get_events(rig.root, types=[EventType.TOOL_RESULT])) == 1


async def test_a_session_whose_worker_stopped_while_the_computer_was_away_is_resumed_when_it_returns(
    laptop_rig, session_factory, redis_client,
):
    rig = laptop_rig
    store, tools = SessionStore(session_factory), builtin_tools()
    io = {"redis_client": redis_client, "session_factory": session_factory}
    args = {"command": "echo once >> log.txt"}
    assistant = {"role": "assistant", "content": "", "tool_calls": [model_call("call_1", "terminal", args)]}
    await store.emit_event(rig.root, EventType.LLM_RESPONSE, {"message": assistant})
    await detached_run(rig, lambda: tool_call(rig, store, tools, "call_1", "terminal", args, **io))
    await take_over(store, rig)
    messages = [{"role": "user", "content": "log it"}, assistant]

    resumed = asyncio.create_task(resume_session(rig, store, tools, messages, **io))
    await asyncio.sleep(0.3)
    assert not resumed.done()
    await rig.laptop.connect()
    await asyncio.wait_for(resumed, 15.0)

    assert (rig.folder / "log.txt").read_text() == "once\n"
    assert messages[2]["tool_call_id"] == "call_1"
    assert len(await store.get_events(rig.root, types=[EventType.TOOL_RESULT])) == 1


async def test_the_process_tool_asks_the_computer(laptop_rig, session_factory, redis_client):
    rig = laptop_rig
    await rig.laptop.connect()
    await tool_call(
        rig, SessionStore(session_factory), builtin_tools(), "call_1", "process", {"action": "list"},
        redis_client=redis_client, session_factory=session_factory,
    )
    assert "list_processes" in rig.laptop.ran
    async with session_factory() as db:
        args = (await db.execute(
            select(DeviceOperation.args)
            .where(DeviceOperation.root_session_id == rig.root, DeviceOperation.kind == "list_processes")
        )).scalar_one()
    assert args == {"task_id": str(rig.root)}


async def test_parallel_tool_calls_keep_their_own_journals(laptop_rig, session_factory, redis_client):
    rig = laptop_rig
    await rig.laptop.connect()
    store, tools = SessionStore(session_factory), builtin_tools()
    io = {"redis_client": redis_client, "session_factory": session_factory}
    await asyncio.gather(
        tool_call(rig, store, tools, "call_a", "write_file", {"path": "a.md", "content": "a"}, **io),
        tool_call(rig, store, tools, "call_b", "write_file", {"path": "b.md", "content": "b"}, **io),
    )
    assert (rig.folder / "a.md").read_text() == "a"
    assert (rig.folder / "b.md").read_text() == "b"


async def another_bound_root(api: Api, device_id: UUID) -> UUID:
    """A second root session bound to *device_id*, as if its app had accepted the binding."""
    root = await device_session(api, device_id)
    ops = DeviceOperations(api.app.state.session_factory, api.app.state.redis)
    await ops.bind(session_id=root, device_id=device_id, folder=FOLDER, nonce=NONCE)
    [bind] = [op for op in await ops.pending(device_id, 1) if op.root_session_id == root]
    assert await ops.complete(device_id, 1, bind.id, bind.digest, {"ok": None}) == "completed"
    return root


async def test_a_computer_with_its_fill_of_waiting_sessions_refuses_another(
    api, session_factory, redis_client, monkeypatch,
):
    monkeypatch.setattr(operations_module, "PARKED_SESSIONS_PER_DEVICE", 1, raising=False)
    issued, first = await bound_device(api)
    device_id = UUID(issued["id"])
    second = await another_bound_root(api, device_id)
    ops = DeviceOperations(session_factory, redis_client)
    waiting = asyncio.create_task(ops.run(request_for(device_id, first)))
    await eventually(lambda: has_pending(ops, device_id))
    with pytest.raises(DeviceOperationError, match="Too many sessions are waiting for Flavius's ThinkPad"):
        await asyncio.wait_for(ops.run(request_for(device_id, second)), 2.0)
    assert [op.calling_session_id for op in await ops.pending(device_id, 1)] == [first]
    await stop(waiting)


async def test_a_waiting_session_may_keep_asking(api, session_factory, redis_client, monkeypatch):
    issued, first = await bound_device(api)
    device_id = UUID(issued["id"])
    second = await another_bound_root(api, device_id)
    ops = DeviceOperations(session_factory, redis_client)
    waits = [asyncio.create_task(ops.run(request_for(device_id, root))) for root in (first, second)]

    async def both_waiting() -> bool:
        return len(await ops.pending(device_id, 1)) == 2

    await eventually(both_waiting)
    monkeypatch.setattr(operations_module, "PARKED_SESSIONS_PER_DEVICE", 1, raising=False)  # now past its fill
    again = asyncio.create_task(ops.run(request_for(device_id, second)))

    async def recorded() -> bool:
        return len(await ops.pending(device_id, 1)) == 3

    await eventually(recorded)
    for task in (*waits, again):
        await stop(task)


async def test_a_binding_never_counts_as_a_waiting_session(api, session_factory, redis_client, monkeypatch):
    monkeypatch.setattr(operations_module, "PARKED_SESSIONS_PER_DEVICE", 1, raising=False)
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client)
    unbound = await device_session(api, device_id)
    await ops.bind(session_id=unbound, device_id=device_id, folder=FOLDER, nonce=NONCE)  # waits for its user
    waiting = asyncio.create_task(ops.run(request_for(device_id, root)))

    async def recorded() -> bool:
        return any(op.calling_session_id == root for op in await ops.pending(device_id, 1))

    await eventually(recorded)
    await stop(waiting)


async def test_a_replayed_operation_gets_its_outcome_on_a_full_computer(
    api, session_factory, redis_client, tmp_path, monkeypatch,
):
    monkeypatch.setattr(operations_module, "PARKED_SESSIONS_PER_DEVICE", 1, raising=False)
    issued, first = await bound_device(api)
    device_id = UUID(issued["id"])
    second = await another_bound_root(api, device_id)
    ops = DeviceOperations(session_factory, redis_client)
    request = request_for(device_id, first)
    done = asyncio.create_task(ops.run(request))
    await eventually(lambda: has_pending(ops, device_id))
    assert await complete_pending(ops, device_id, LocalWorkspaceIO(str(tmp_path))) == 1
    assert await asyncio.wait_for(done, 2.0) == {"ok": True}
    parked = asyncio.create_task(ops.run(request_for(device_id, second)))  # the computer is now full
    await eventually(lambda: has_pending(ops, device_id))
    # The first session's worker restarted and asks again: it gets what happened.
    assert await asyncio.wait_for(ops.run(request), 2.0) == {"ok": True}
    await stop(parked)


async def test_a_call_already_under_way_finishes_on_a_full_computer(
    api, session_factory, redis_client, tmp_path, monkeypatch,
):
    monkeypatch.setattr(operations_module, "PARKED_SESSIONS_PER_DEVICE", 1, raising=False)
    issued, first = await bound_device(api)
    device_id = UUID(issued["id"])
    second = await another_bound_root(api, device_id)
    ops = DeviceOperations(session_factory, redis_client)
    request = request_for(device_id, first)  # the first step of a multi-step tool call
    done = asyncio.create_task(ops.run(request))
    await eventually(lambda: has_pending(ops, device_id))
    assert await complete_pending(ops, device_id, LocalWorkspaceIO(str(tmp_path))) == 1
    assert await asyncio.wait_for(done, 2.0) == {"ok": True}
    parked = asyncio.create_task(ops.run(request_for(device_id, second)))  # the computer is now full
    await eventually(lambda: has_pending(ops, device_id))
    # Between its steps the first session has nothing open, but its call may still finish.
    next_step = asyncio.create_task(ops.run(OperationRequest(**{**_fields(request), "ordinal": 2})))

    async def recorded() -> bool:
        if next_step.done():
            next_step.result()  # a refusal surfaces here
        return len(await ops.pending(device_id, 1)) == 2

    await eventually(recorded)
    for task in (parked, next_step):
        await stop(task)


async def test_a_tool_call_waits_for_the_computer_to_come_back(laptop_rig, session_factory, redis_client):
    rig = laptop_rig  # the laptop starts disconnected
    store = SessionStore(session_factory)
    call = asyncio.create_task(tool_call(
        rig, store, builtin_tools(), "call_1", "write_file", {"path": "late.md", "content": "late"},
        redis_client=redis_client, session_factory=session_factory,
    ))
    await asyncio.sleep(0.5)
    assert not call.done()
    await rig.laptop.connect()
    await asyncio.wait_for(call, 15.0)
    assert (rig.folder / "late.md").read_text() == "late"


async def test_an_operation_waits_for_the_laptop_to_connect(laptop_rig):
    rig = laptop_rig
    wio = device_io(rig.ops, rig.device_id, rig.root, rig.folder)
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
    wio = device_io(rig.ops, rig.device_id, rig.root, rig.folder)
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

    wio = device_io(DeviceOperations(session_factory, Silent()), rig.device_id, rig.root, rig.folder)
    assert await asyncio.wait_for(wio.which("sh"), 5.0) is True


async def test_a_reply_for_another_devices_operation_is_rejected_and_closes_the_link(api, link_url, laptop_rig):
    rig = laptop_rig
    other = await register(api, name="Other laptop")
    waiting = asyncio.create_task(device_io(rig.ops, rig.device_id, rig.root, rig.folder).which("sh"))
    await eventually(lambda: has_pending(rig.ops, rig.device_id))
    [op] = await rig.ops.pending(rig.device_id, 1)
    async with linked(link_url, other["token"]) as (ws, _):
        await send(ws, {"type": "op_result", "id": str(op.id), "digest": op.digest, "outcome": {"ok": True}})
        # Named, so the app drops that reply rather than send it again at every welcome.
        assert await receive(ws) == {"type": "rejected", "id": str(op.id)}
        assert await close_code(ws) == 4400
    assert await rig.ops.pending(rig.device_id, 1) != []
    await stop(waiting)


async def test_a_result_header_for_another_devices_read_is_rejected_open_or_closed(api, link_url, laptop_rig):
    rig = laptop_rig
    other = await register(api, name="Other laptop")
    reading = asyncio.create_task(device_io(rig.ops, rig.device_id, rig.root, rig.folder).read("a.txt"))
    await eventually(lambda: has_pending(rig.ops, rig.device_id))
    [op] = await rig.ops.pending(rig.device_id, 1)
    header = {
        "type": "op_result", "id": str(op.id), "digest": op.digest,
        "outcome": {"ok": {"transfer": {"size": workspace_module.MAX_PAYLOAD_BYTES + 1, "sha256": "0" * 64}}},
    }
    async with linked(link_url, other["token"]) as (ws, _):
        await send(ws, header)
        assert await receive(ws) == {"type": "rejected", "id": str(op.id)}
        assert await close_code(ws) == 4400
    await stop(reading)  # closed now, cancelled
    assert await rig.ops.pending(rig.device_id, 1) == []
    async with linked(link_url, other["token"]) as (ws, _):
        await send(ws, header)
        # Refused, as for an open one: whether another device's operation is closed is not its to learn.
        assert await receive(ws) == {"type": "rejected", "id": str(op.id)}
        assert await close_code(ws) == 4400


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
    waiting = asyncio.create_task(device_io(rig.ops, rig.device_id, rig.root, rig.folder).which("sh"))
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
    waiting = asyncio.create_task(device_io(rig.ops, rig.device_id, rig.root, rig.folder).which("sh"))
    async with linked(link_url, rig.token) as (ws, _):
        op = await _first_op(ws)
        reply = {"type": "op_result", "id": op["id"], "digest": op["digest"], "outcome": {"ok": True}}
        await send(ws, reply)
        assert await receive(ws) == {"type": "op_ack", "id": op["id"]}
        await send(ws, reply)
        assert await receive(ws) == {"type": "op_ack", "id": op["id"]}
    assert await asyncio.wait_for(waiting, 2.0) is True


async def test_a_refused_operation_does_not_wedge_the_link(laptop_rig):
    rig = laptop_rig
    await rig.laptop.connect()
    wio = device_io(rig.ops, rig.device_id, rig.root, rig.folder)
    with pytest.raises(ValueError, match="valid Unicode"):
        await asyncio.wait_for(wio.which("sh\ud83d"), 2.0)
    assert await asyncio.wait_for(wio.which("sh"), 5.0) is True
    assert rig.laptop.ran == ["which"]
    assert rig.laptop.connected


async def test_a_result_that_is_not_valid_unicode_is_recorded_as_an_error(laptop_rig, link_url):
    rig = laptop_rig
    waiting = asyncio.create_task(device_io(rig.ops, rig.device_id, rig.root, rig.folder).which("sh"))
    async with linked(link_url, rig.token) as (ws, _):
        op = await _first_op(ws)
        await send(ws, {
            "type": "op_result", "id": op["id"], "digest": op["digest"],
            "outcome": {"ok": "half a pair: \ud83d"},
        })
        # Acknowledged, not closed: the app sends its journaled reply again on
        # every reconnect, so a refusal would loop.
        assert await receive(ws) == {"type": "op_ack", "id": op["id"]}
        await send(ws, {"type": "ping"})
        assert await receive(ws) == {"type": "pong"}
    with pytest.raises(DeviceOperationError, match="not valid Unicode"):
        await asyncio.wait_for(waiting, 2.0)
    assert await rig.ops.pending(rig.device_id, 1) == []


async def test_old_credentials_get_no_operations_and_cannot_reply(
    api, laptop_rig, link_url, session_factory,
):
    rig = laptop_rig
    async with linked(link_url, rig.token) as (ws, _):
        # Rotate in the database only, as if the rotation notice were lost.
        await DeviceStore(session_factory).reauthorize(
            rig.device_id, org_id=api.org_id, agent_id=AGENT_ID, user_id=api.user_id,
        )
        waiting = asyncio.create_task(device_io(rig.ops, rig.device_id, rig.root, rig.folder).which("sh"))
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
            waiting = asyncio.create_task(device_io(rig.ops, rig.device_id, rig.root, rig.folder).which("sh"))
            op = await _first_op(new)
            assert op["kind"] == "which"
            await stop(waiting)


async def test_a_pong_does_not_wait_for_the_delivery_after_it(api, link_url, monkeypatch):
    monkeypatch.setattr(link_module, "MIN_REFRESH_INTERVAL_S", 0.0)
    issued = await register(api)
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_delivery(self):
        started.set()
        await release.wait()

    async with linked(link_url, issued["token"]) as (ws, _):
        monkeypatch.setattr(link_module._Link, "deliver", slow_delivery)
        await send(ws, {"type": "ping"})
        # The delivery cannot finish until the test releases it, so a pong
        # that waited for it would never arrive.
        try:
            assert await receive(ws) == {"type": "pong"}
            await asyncio.wait_for(started.wait(), 2.0)
        finally:
            release.set()


def slow_deliveries(monkeypatch) -> tuple[asyncio.Event, asyncio.Event]:
    """Make every delivery batch stay in flight until the test releases it."""
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(self):
        started.set()
        await release.wait()

    monkeypatch.setattr(link_module._Link, "deliver", slow)
    return started, release


async def test_a_ping_is_answered_during_the_delivery_at_welcome(api, link_url, monkeypatch):
    issued = await register(api)
    started, release = slow_deliveries(monkeypatch)
    async with linked(link_url, issued["token"]) as (ws, _):
        try:
            # The offline backlog is being sent; the app's heartbeat must not wait for it.
            await asyncio.wait_for(started.wait(), 2.0)
            await send(ws, {"type": "ping"})
            assert await receive(ws, timeout=2.0) == {"type": "pong"}
        finally:
            release.set()


async def test_pings_are_answered_during_a_delivery_that_a_ping_started(api, link_url, monkeypatch):
    monkeypatch.setattr(link_module, "MIN_REFRESH_INTERVAL_S", 0.0)
    issued = await register(api)
    async with linked(link_url, issued["token"]) as (ws, _):
        started, release = slow_deliveries(monkeypatch)
        try:
            await send(ws, {"type": "ping"})
            assert await receive(ws, timeout=2.0) == {"type": "pong"}
            await asyncio.wait_for(started.wait(), 2.0)
            await send(ws, {"type": "ping"})
            assert await receive(ws, timeout=2.0) == {"type": "pong"}
        finally:
            release.set()


async def test_a_revocation_closes_the_link_during_a_delivery(api, link_url, redis_client, monkeypatch):
    issued = await register(api)
    async with linked(link_url, issued["token"]) as (ws, _):
        started, release = slow_deliveries(monkeypatch)
        try:
            # A worker's announcement starts a delivery that does not finish.
            await DevicePresence(redis_client).publish(UUID(issued["id"]), f"op:{uuid.uuid4()}")
            await asyncio.wait_for(started.wait(), 2.0)
            await api.client.delete(f"/v1/devices/{issued['id']}", headers=api.auth())
            assert await close_code(ws, timeout=2.0) == 4403
        finally:
            release.set()


async def test_an_announcement_during_a_delivery_is_delivered_after_it(
    api, link_url, redis_client, monkeypatch,
):
    issued = await register(api)
    calls: list[int] = []
    first_started, release = asyncio.Event(), asyncio.Event()

    async def deliver(self):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            first_started.set()
            await release.wait()

    monkeypatch.setattr(link_module._Link, "deliver", deliver)
    async with linked(link_url, issued["token"]):
        try:
            await asyncio.wait_for(first_started.wait(), 2.0)  # the delivery at welcome
            await DevicePresence(redis_client).publish(UUID(issued["id"]), f"op:{uuid.uuid4()}")
            await asyncio.sleep(0.3)
            assert calls == [1]
        finally:
            release.set()

        async def delivered_again() -> bool:
            return len(calls) == 2

        await eventually(delivered_again)


def noticing_journal(session_factory, redis_client) -> DeviceOperations:
    """A journal that tells sessions when their computer is away, quickly."""
    return DeviceOperations(
        session_factory, redis_client, recheck_interval_s=0.1,
        notice=DeviceWaitNotice(SessionStore(session_factory, redis_client), session_factory),
    )


async def device_wait_events(session_factory, session_id: UUID) -> list[tuple[str, dict]]:
    events = await SessionStore(session_factory).get_events(
        session_id, types=[EventType.DEVICE_WAITING, EventType.DEVICE_RESUMED],
    )
    return [(e.type, e.data) for e in events]  # Event.type is the str value


def waiting_for(rig) -> tuple[str, dict]:
    return ("device.waiting", {
        "device_id": str(rig.device_id), "device_name": "Flavius's ThinkPad", "reason": "offline",
    })


def resumed_from(rig) -> tuple[str, dict]:
    return ("device.resumed", {"device_id": str(rig.device_id)})


async def test_a_session_waiting_for_an_absent_computer_says_so(
    laptop_rig, session_factory, redis_client, monkeypatch,
):
    monkeypatch.setattr(operations_module, "WAIT_GRACE_S", 0.1)
    rig = laptop_rig  # the laptop starts disconnected
    ops = noticing_journal(session_factory, redis_client)
    waiting = asyncio.create_task(ops.run(request_for(rig.device_id, rig.root)))

    async def says_waiting() -> bool:
        return await device_wait_events(session_factory, rig.root) == [waiting_for(rig)]

    await eventually(says_waiting)
    await rig.laptop.connect()
    assert await asyncio.wait_for(waiting, 5.0) == {"ok": True}
    assert await device_wait_events(session_factory, rig.root) == [waiting_for(rig), resumed_from(rig)]


async def test_two_waiting_operations_of_a_session_say_so_once(
    laptop_rig, session_factory, redis_client, monkeypatch,
):
    monkeypatch.setattr(operations_module, "WAIT_GRACE_S", 0.1)
    rig = laptop_rig
    ops = noticing_journal(session_factory, redis_client)
    requests = [request_for(rig.device_id, rig.root) for _ in range(2)]
    waits = [asyncio.create_task(ops.run(request)) for request in requests]

    async def says_waiting() -> bool:
        return await device_wait_events(session_factory, rig.root) == [waiting_for(rig)]

    await eventually(says_waiting)
    await asyncio.sleep(0.3)  # both are past their grace now
    assert await device_wait_events(session_factory, rig.root) == [waiting_for(rig)]
    # One of them is answered while the computer is still away: the other still waits.
    [answered] = [op for op in await ops.pending(rig.device_id, 1) if op.invocation_id == requests[0].invocation_id]
    assert await ops.complete(rig.device_id, 1, answered.id, answered.digest, {"ok": True}) == "completed"
    assert await asyncio.wait_for(waits[0], 5.0) == {"ok": True}
    assert not waits[1].done()
    assert await device_wait_events(session_factory, rig.root) == [waiting_for(rig)]
    await rig.laptop.connect()
    assert await asyncio.wait_for(waits[1], 5.0) == {"ok": True}
    assert await device_wait_events(session_factory, rig.root) == [waiting_for(rig), resumed_from(rig)]


async def test_a_stopped_wait_says_the_computer_is_no_longer_awaited(
    laptop_rig, session_factory, redis_client, monkeypatch,
):
    monkeypatch.setattr(operations_module, "WAIT_GRACE_S", 0.1)
    rig = laptop_rig  # the laptop stays disconnected
    ops = noticing_journal(session_factory, redis_client)
    waiting = asyncio.create_task(ops.run(request_for(rig.device_id, rig.root)))

    async def says_waiting() -> bool:
        return await device_wait_events(session_factory, rig.root) == [waiting_for(rig)]

    await eventually(says_waiting)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert await device_wait_events(session_factory, rig.root) == [waiting_for(rig), resumed_from(rig)]


async def test_a_detached_wait_leaves_the_session_waiting(laptop_rig, session_factory, redis_client, monkeypatch):
    monkeypatch.setattr(operations_module, "WAIT_GRACE_S", 0.1)
    rig = laptop_rig  # the laptop stays disconnected
    ops = noticing_journal(session_factory, redis_client)
    turn: list[TurnSlots] = []

    async def run_turn() -> None:
        slots = TurnSlots(
            semaphore=asyncio.Semaphore(1), gate=None, org_id="", agent_id="",
            gate_held=False, task=asyncio.current_task(),
        )
        turn.append(slots)
        current_turn.set(slots)
        async with slots.activity():
            await ops.run(request_for(rig.device_id, rig.root))

    async def says_waiting() -> bool:
        return await device_wait_events(session_factory, rig.root) == [waiting_for(rig)]

    task = asyncio.create_task(run_turn())
    await eventually(says_waiting)
    turn[0].detach()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    # Another worker carries the wait on: the operation stays open and the session still waits.
    assert await has_pending(rig.ops, rig.device_id)
    assert await device_wait_events(session_factory, rig.root) == [waiting_for(rig)]


async def outcome_of(session_factory, root: UUID) -> dict | None:
    async with session_factory() as db:
        return (await db.execute(
            select(DeviceOperation.outcome)
            .where(DeviceOperation.root_session_id == root, DeviceOperation.kind != BIND)
            .order_by(DeviceOperation.created_at.desc())
            .limit(1)
        )).scalar_one()


async def detached_run(rig, call) -> None:
    """Run *call* in a turn that is then handed over, so what it left open stays open."""
    turn: list[TurnSlots] = []

    async def worker() -> None:
        slots = TurnSlots(
            semaphore=asyncio.Semaphore(1), gate=None, org_id="", agent_id="",
            gate_held=False, task=asyncio.current_task(),
        )
        turn.append(slots)
        current_turn.set(slots)
        async with slots.activity():
            await call()

    first = asyncio.create_task(worker())
    await eventually(lambda: has_pending(rig.ops, rig.device_id))
    turn[0].detach()
    await asyncio.gather(first, return_exceptions=True)


async def test_cancelling_a_session_ends_its_wait_and_tells_its_computer(laptop_rig, session_factory, redis_client):
    rig = laptop_rig  # the laptop stays away
    control = await DevicePresence(redis_client).subscribe(rig.device_id)
    try:
        waiting = asyncio.create_task(rig.ops.run(request_for(rig.device_id, rig.root)))
        announced = await next_control(control)
        assert announced.startswith("op:")
        assert await rig.ops.cancel([rig.root]) == 1
        assert await asyncio.wait_for(waiting, 5.0) == CANCELLED_OUTCOME
        assert await next_control(control) == f"cancel:{announced.removeprefix('op:')}"
    finally:
        await control.aclose()
    assert not await has_pending(rig.ops, rig.device_id)
    assert await outcome_of(session_factory, rig.root) == CANCELLED_OUTCOME


async def test_a_finished_operation_keeps_its_outcome_when_its_session_is_cancelled(laptop_rig, session_factory):
    rig = laptop_rig
    await rig.laptop.connect()
    done = await asyncio.wait_for(rig.ops.run(request_for(rig.device_id, rig.root)), 10.0)
    assert await rig.ops.cancel([rig.root]) == 0
    assert await outcome_of(session_factory, rig.root) == done


async def test_a_binding_is_cancelled_only_when_asked(api, session_factory, redis_client):
    issued = await register(api, "Laptop")
    device_id = UUID(issued["id"])
    root = await device_session(api, device_id)
    ops = DeviceOperations(session_factory, redis_client)
    await ops.bind(session_id=root, device_id=device_id, folder=FOLDER, nonce=NONCE)  # never answered
    assert await ops.cancel([root]) == 0
    assert (await binding(api, root)).state == "pending"
    assert await ops.cancel([root], bindings=True) == 1
    assert (await binding(api, root)).state == "failed"


async def test_a_stopped_call_closes_its_operation(laptop_rig, session_factory):
    rig = laptop_rig  # the laptop stays away
    waiting = asyncio.create_task(rig.ops.run(request_for(rig.device_id, rig.root)))
    await eventually(lambda: has_pending(rig.ops, rig.device_id))
    await stop(waiting)
    assert not await has_pending(rig.ops, rig.device_id)
    assert await outcome_of(session_factory, rig.root) == CANCELLED_OUTCOME


async def test_a_call_stopped_while_its_operation_is_announced_closes_it(laptop_rig, session_factory, monkeypatch):
    rig = laptop_rig
    announcing = asyncio.Event()
    real_announce = DeviceOperations._announce

    async def slow_announce(self, channel, message):
        if message.startswith("op:"):
            announcing.set()
            await asyncio.sleep(1.0)
        await real_announce(self, channel, message)

    monkeypatch.setattr(DeviceOperations, "_announce", slow_announce)
    waiting = asyncio.create_task(rig.ops.run(request_for(rig.device_id, rig.root)))
    await asyncio.wait_for(announcing.wait(), 5.0)
    await stop(waiting)
    assert not await has_pending(rig.ops, rig.device_id)


async def test_a_call_stopped_while_its_record_commits_closes_its_operation(laptop_rig, session_factory, monkeypatch):
    rig = laptop_rig
    recorded = asyncio.Event()
    real_record = DeviceOperations._record

    async def record_then_linger(self, request):
        found = await real_record(self, request)  # the row is committed
        recorded.set()
        await asyncio.Event().wait()  # the reply has not reached the call yet
        return found

    monkeypatch.setattr(DeviceOperations, "_record", record_then_linger)
    waiting = asyncio.create_task(rig.ops.run(request_for(rig.device_id, rig.root)))
    await asyncio.wait_for(recorded.wait(), 5.0)
    await stop(waiting)
    assert not await has_pending(rig.ops, rig.device_id)
    assert await outcome_of(session_factory, rig.root) == CANCELLED_OUTCOME


async def test_a_stopped_call_closes_its_wait_before_it_returns(laptop_rig, monkeypatch):
    rig = laptop_rig
    closed: list[UUID] = []

    async def slow_to_close(self, operation_id):
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.05)  # as closing the subscription does
            closed.append(operation_id)

    monkeypatch.setattr(DeviceOperations, "_wait_forever", slow_to_close)
    waiting = asyncio.create_task(rig.ops.run(request_for(rig.device_id, rig.root)))
    await eventually(lambda: has_pending(rig.ops, rig.device_id))
    await stop(waiting)
    assert len(closed) == 1


async def test_a_stopped_session_records_nothing_new(laptop_rig, session_factory):
    rig = laptop_rig
    await SessionStore(session_factory).update_session_status(rig.root, "paused")
    with pytest.raises(DeviceOperationError, match="This session was stopped"):
        await asyncio.wait_for(rig.ops.run(request_for(rig.device_id, rig.root)), 5.0)
    assert await operation_rows(session_factory, rig.device_id) == []


async def test_a_call_resumed_as_a_pause_lands_still_learns_what_the_computer_did(
    laptop_rig, session_factory, redis_client,
):
    rig = laptop_rig
    await rig.laptop.connect()
    store, tools = SessionStore(session_factory), builtin_tools()
    io = {"redis_client": redis_client, "session_factory": session_factory}
    args = {"command": "echo once >> log.txt"}
    await tool_call(rig, store, tools, "call_1", "terminal", args, **io)
    await forget_result(store, session_factory, rig.root, "call_1")
    await take_over(store, rig)
    await store.update_session_status(rig.root, "paused")  # the pause lands after the new worker's wake began

    resumed = await resume_call(rig, store, tools, "call_1", "terminal", args, **io)

    assert json.loads(resumed["content"])["exit_code"] == 0
    assert (rig.folder / "log.txt").read_text() == "once\n"


async def test_a_call_reported_interrupted_closes_what_its_first_run_left_open(
    laptop_rig, session_factory, redis_client, monkeypatch,
):
    rig = laptop_rig  # the laptop stays away
    store, tools = SessionStore(session_factory), builtin_tools()
    io = {"redis_client": redis_client, "session_factory": session_factory}

    async def handler(arguments: dict, **kwargs) -> str:
        wio = kwargs["workspace_io"]
        await wio.read(await wio.resolve("notes.md"))  # waits for the computer
        return json.dumps({"ok": True})

    tools.register(
        "harness_probe",
        ToolSchema(name="harness_probe", description="probe", parameters={"type": "object", "properties": {}}),
        handler=handler,
    )
    monkeypatch.setitem(TOOL_LOCATIONS, "harness_probe", ToolLocation.HARNESS)
    await detached_run(rig, lambda: tool_call(rig, store, tools, "call_1", "harness_probe", {}, **io))
    assert await has_pending(rig.ops, rig.device_id)  # its first run left the read open
    await take_over(store, rig)

    resumed = await resume_call(rig, store, tools, "call_1", "harness_probe", {}, **io)

    assert resumed["content"] == INTERRUPTED
    assert not await has_pending(rig.ops, rig.device_id)
    assert await outcome_of(session_factory, rig.root) == CANCELLED_OUTCOME


async def test_a_cancellation_reaches_a_connected_computer(laptop_rig):
    rig = laptop_rig
    rig.laptop.hold = True  # it receives the operation and keeps working on it
    await rig.laptop.connect()
    waiting = asyncio.create_task(rig.ops.run(request_for(rig.device_id, rig.root)))

    async def received() -> bool:
        return len(rig.laptop.received) == 1

    await eventually(received)
    await rig.ops.cancel([rig.root])
    assert await asyncio.wait_for(waiting, 5.0) == CANCELLED_OUTCOME

    async def told() -> bool:
        return rig.laptop.cancelled == set(rig.laptop.received)

    await eventually(told)
    assert rig.laptop.ran == []


async def test_a_lost_cancellation_reaches_the_computer_at_the_next_reconcile(laptop_rig, monkeypatch):
    rig = laptop_rig
    rig.laptop.hold = True
    await rig.laptop.connect()
    waiting = asyncio.create_task(rig.ops.run(request_for(rig.device_id, rig.root)))

    async def received() -> bool:
        return len(rig.laptop.received) == 1

    await eventually(received)
    real_announce = DeviceOperations._announce

    async def lose_cancels(self, channel, message):
        if not message.startswith("cancel:"):
            await real_announce(self, channel, message)

    monkeypatch.setattr(DeviceOperations, "_announce", lose_cancels)
    await rig.ops.cancel([rig.root])
    await asyncio.wait_for(waiting, 5.0)

    async def told() -> bool:
        return rig.laptop.cancelled == set(rig.laptop.received)

    await eventually(told, timeout=5.0)  # the laptop pings every 0.2 s; a reconcile follows within 1 s


async def test_a_computer_hears_of_a_cancellation_first_when_it_reconnects(laptop_rig):
    rig = laptop_rig
    rig.laptop.hold = True
    await rig.laptop.connect()
    first = asyncio.create_task(rig.ops.run(request_for(rig.device_id, rig.root)))

    async def received() -> bool:
        return len(rig.laptop.received) == 1

    await eventually(received)
    await rig.laptop.disconnect()
    await rig.ops.cancel([rig.root])
    await asyncio.wait_for(first, 5.0)
    second = asyncio.create_task(rig.ops.run(request_for(rig.device_id, rig.root, args={"name": "bash"})))
    await eventually(lambda: has_pending(rig.ops, rig.device_id))

    rig.laptop.frames.clear()
    await rig.laptop.connect()

    async def both() -> bool:
        return "cancel" in rig.laptop.frames and "op" in rig.laptop.frames

    await eventually(both)
    assert rig.laptop.frames.index("cancel") < rig.laptop.frames.index("op")
    assert rig.laptop.cancelled == {rig.laptop.received[0]}
    await stop(second)


async def test_a_computer_hears_first_that_a_revocation_closed_what_it_held(laptop_rig, api):
    rig = laptop_rig
    rig.laptop.hold = True
    await rig.laptop.connect()
    first = asyncio.create_task(rig.ops.run(request_for(rig.device_id, rig.root)))

    async def received() -> bool:
        return len(rig.laptop.received) == 1

    await eventually(received)
    await rig.laptop.disconnect()
    assert (await api.client.delete(f"/v1/devices/{rig.device_id}", headers=api.auth())).status_code == 204
    await stop(first)  # the wait would see the revocation at its next recheck
    reauthorized = await api.client.post(f"/v1/devices/{rig.device_id}/reauthorize", headers=api.auth())
    assert reauthorized.status_code == 200, reauthorized.text
    rig.laptop.token = reauthorized.json()["token"]
    second = asyncio.create_task(rig.ops.run(request_for(rig.device_id, rig.root, args={"name": "bash"})))
    await eventually(lambda: has_pending(rig.ops, rig.device_id, 2))

    rig.laptop.frames.clear()
    await rig.laptop.connect()  # its hello still lists the operation the revocation closed

    async def both() -> bool:
        return "cancel" in rig.laptop.frames and "op" in rig.laptop.frames

    await eventually(both)
    assert rig.laptop.frames.index("cancel") < rig.laptop.frames.index("op")
    assert rig.laptop.cancelled == {rig.laptop.received[0]}
    await stop(second)


async def test_only_closed_operations_are_found_among_a_computers_own(laptop_rig, api):
    rig = laptop_rig  # the laptop stays away
    names = ["cancelled", "open"]
    requests = {name: request_for(rig.device_id, rig.root, args={"name": name}) for name in names}
    waiting = [asyncio.create_task(rig.ops.run(request)) for request in requests.values()]

    async def all_recorded() -> bool:
        return len(await rig.ops.pending(rig.device_id, 1)) == len(names)

    await eventually(all_recorded)
    by_name = {op.args["name"]: op for op in await rig.ops.pending(rig.device_id, 1)}
    await rig.ops.cancel_invocation(rig.root, requests["cancelled"].invocation_id)
    ids = [op.id for op in by_name.values()]

    assert await rig.ops.closed_among(rig.device_id, ids) == [by_name["cancelled"].id]
    assert await rig.ops.closed_among(rig.device_id, []) == []
    # A revocation closes what is still open, and the computer must hear of those too.
    assert (await api.client.delete(f"/v1/devices/{rig.device_id}", headers=api.auth())).status_code == 204
    assert set(await rig.ops.closed_among(rig.device_id, ids)) == set(ids)
    await asyncio.gather(*(stop(task) for task in waiting))


@pytest.mark.parametrize("reported", ["not a list", ["not-a-uuid"], [str(uuid.uuid4())] * 1001])
async def test_a_hello_with_a_malformed_open_list_is_refused(laptop_rig, link_url, reported):
    async with connect(
        link_url, additional_headers={"Authorization": f"Bearer {laptop_rig.token}"},
    ) as ws:
        await ws.send(json.dumps({"type": "hello", "protocols": [1], "open": reported}))
        with pytest.raises(ConnectionClosed) as closed:
            await ws.recv()
        assert closed.value.rcvd.code == 4400


async def test_a_computer_lost_after_delivery_is_waited_for_too(
    laptop_rig, session_factory, redis_client, monkeypatch,
):
    monkeypatch.setattr(operations_module, "WAIT_GRACE_S", 0.1)
    rig = laptop_rig
    rig.laptop.reply = False  # it receives the operation, then drops off
    await rig.laptop.connect()
    ops = noticing_journal(session_factory, redis_client)
    waiting = asyncio.create_task(ops.run(request_for(rig.device_id, rig.root)))

    async def says_waiting() -> bool:
        return await device_wait_events(session_factory, rig.root) == [waiting_for(rig)]

    await eventually(says_waiting, timeout=5.0)
    rig.laptop.reply = True
    await rig.laptop.disconnect()  # clear the dropped connection's tasks before connecting again
    await rig.laptop.connect()
    assert await asyncio.wait_for(waiting, 5.0) == {"ok": True}
    assert await device_wait_events(session_factory, rig.root) == [waiting_for(rig), resumed_from(rig)]


async def test_a_quick_answer_tells_no_one(laptop_rig, session_factory, redis_client):
    rig = laptop_rig
    await rig.laptop.connect()
    ops = noticing_journal(session_factory, redis_client)
    assert await asyncio.wait_for(ops.run(request_for(rig.device_id, rig.root)), 5.0) == {"ok": True}
    assert await device_wait_events(session_factory, rig.root) == []


class UnreachableDatabase:
    """A session factory whose sessions fail the moment they are used."""

    def __call__(self):
        return self

    async def __aenter__(self):
        raise OperationalError("SELECT 1", {}, ConnectionError("database unreachable"))

    async def __aexit__(self, *exc) -> None:
        return None


async def test_a_failed_name_lookup_does_not_fail_the_wait_notice(laptop_rig, session_factory, redis_client):
    rig = laptop_rig
    request = request_for(rig.device_id, rig.root)
    notice = DeviceWaitNotice(SessionStore(session_factory, redis_client), UnreachableDatabase())
    await notice.away(request)  # must not raise
    try:
        events = await device_wait_events(session_factory, rig.root)
        assert events == [("device.waiting", {
            "device_id": str(rig.device_id), "device_name": "your computer", "reason": "offline",
        })]
    finally:
        await notice.back(request)  # leave the process-wide count as it was


async def test_pausing_a_local_chat_cancels_what_waits_on_its_computer(laptop_rig, api):
    rig = laptop_rig  # the laptop stays away
    waiting = asyncio.create_task(rig.ops.run(request_for(rig.device_id, rig.root)))
    await eventually(lambda: has_pending(rig.ops, rig.device_id))
    paused = await api.client.post(f"/v1/sessions/{rig.root}/pause", headers=api.auth())
    assert paused.status_code == 200, paused.text
    assert await asyncio.wait_for(waiting, 5.0) == CANCELLED_OUTCOME
    with pytest.raises(DeviceOperationError, match="This session was stopped"):
        await asyncio.wait_for(rig.ops.run(request_for(rig.device_id, rig.root, args={"name": "bash"})), 5.0)


async def test_a_paused_chat_reads_paused_when_its_pause_event_is_written(laptop_rig, api, session_factory, monkeypatch):
    rig = laptop_rig
    store = api.app.state.session_store
    seen_with_the_event = text(
        "SELECT s.status FROM sessions s WHERE s.id = :id "
        "AND EXISTS (SELECT 1 FROM events e WHERE e.session_id = s.id AND e.type = 'session.pause')"
    )

    async def status_where_the_event_is() -> str | None:
        # One statement on a connection of its own: the status as a reader that can see the event sees it.
        async with session_factory() as db:
            return (await db.execute(seen_with_the_event, {"id": rig.root})).scalar()

    # A write of the status apart from the event's would leave a paused chat with no event, if the event's
    # write then failed: with the event's write lost, no reader sees either.
    emit = store.emit_event

    async def the_events_write_is_lost(*args, **kwargs):
        raise ConnectionError("the database went away")

    with monkeypatch.context() as lost:
        lost.setattr(store, "emit_event", the_events_write_is_lost)
        with pytest.raises(ConnectionError):
            await api.client.post(f"/v1/sessions/{rig.root}/pause", headers=api.auth())
    assert (await store.get_session(rig.root)).status == "active" and await status_where_the_event_is() is None
    monkeypatch.setattr(store, "emit_event", emit)

    # Where a reader stands.  At the event's announcement, which a waiting call and the stream wake on:
    announced: list[str | None] = []

    async def reading_the_status(channel, message):
        if str(message).endswith(f":{EventType.SESSION_PAUSE.value}"):
            announced.append(await status_where_the_event_is())

    # The store announces an event once it is committed; here to a listener that reads at once.
    monkeypatch.setattr(store, "_redis", SimpleNamespace(publish=reading_the_status))
    # And from a second connection that polls for the event's row all through the pause.
    polled: list[str] = []

    async def polling() -> None:
        while True:
            status = await status_where_the_event_is()
            if status is not None:
                polled.append(status)
            await asyncio.sleep(0)

    poller = asyncio.create_task(polling())
    try:
        paused = await api.client.post(f"/v1/sessions/{rig.root}/pause", headers=api.auth())
        await asyncio.sleep(0.2)
    finally:
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)
    assert paused.status_code == 200, paused.text
    # No reader that can see the session.pause event sees a status other than paused.
    assert announced == ["paused"]
    assert polled and set(polled) == {"paused"}


async def test_a_chat_resumed_after_a_pause_reports_its_call_stopped(laptop_rig, api, session_factory, redis_client):
    rig = laptop_rig  # the laptop stays away
    store, tools = SessionStore(session_factory), builtin_tools()
    io = {"redis_client": redis_client, "session_factory": session_factory}
    args = {"command": "echo once >> log.txt"}
    await detached_run(rig, lambda: tool_call(rig, store, tools, "call_1", "terminal", args, **io))
    await take_over(store, rig)
    assert (await api.client.post(f"/v1/sessions/{rig.root}/pause", headers=api.auth())).status_code == 200
    assert (await api.client.post(f"/v1/sessions/{rig.root}/resume", headers=api.auth())).status_code == 200

    resumed = await resume_call(rig, store, tools, "call_1", "terminal", args, **io)
    await rig.laptop.connect()
    await asyncio.sleep(0.5)

    assert "Stopped before the computer reported a result" in resumed["content"]
    assert not (rig.folder / "log.txt").exists()


async def test_a_pause_waits_for_a_recording_in_flight_and_cancels_it(laptop_rig, api, session_factory, monkeypatch):
    rig = laptop_rig  # the laptop stays away
    recording, release = asyncio.Event(), asyncio.Event()
    real_refuse_when_full = operations_module._refuse_when_full

    async def refuse_when_full_after_a_wait(db, request, device):
        # Inside the record's transaction, past its session check: its locks are held.
        recording.set()
        await release.wait()
        await real_refuse_when_full(db, request, device)

    monkeypatch.setattr(operations_module, "_refuse_when_full", refuse_when_full_after_a_wait)
    running = asyncio.create_task(rig.ops.run(request_for(rig.device_id, rig.root)))
    pausing: asyncio.Task | None = None
    try:
        await asyncio.wait_for(recording.wait(), 5.0)
        pausing = asyncio.create_task(api.client.post(f"/v1/sessions/{rig.root}/pause", headers=api.auth()))
        await asyncio.sleep(0.5)
        # Its status update waits for the recording's share lock.  Without the
        # lock the pause would finish first, cancel nothing, and the operation
        # recorded after it would run on the computer.
        assert not pausing.done()
        release.set()
        paused = await asyncio.wait_for(pausing, 5.0)
        assert paused.status_code == 200, paused.text
        assert await asyncio.wait_for(running, 5.0) == CANCELLED_OUTCOME
        assert await outcome_of(session_factory, rig.root) == CANCELLED_OUTCOME
    finally:
        release.set()
        await stop(running)
        if pausing is not None:
            await asyncio.gather(pausing, return_exceptions=True)


async def test_a_recording_waits_for_a_delete_in_flight_and_is_refused(laptop_rig, api, session_factory, monkeypatch):
    rig = laptop_rig  # the laptop stays away
    archiving, release = asyncio.Event(), asyncio.Event()
    real_execute = AsyncSession.execute

    async def execute(self, statement, *args, **kwargs):
        # The statement that follows the archive's lock on the tree's sessions.
        if isinstance(statement, Delete) and statement.table.name == "scheduled_sessions":
            archiving.set()
            await release.wait()
        return await real_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", execute)
    deleting = asyncio.create_task(api.app.state.session_store.archive_session_tree_and_delete_schedules(
        rig.root, org_id=api.org_id, agent_id=AGENT_ID,
    ))
    running: asyncio.Task | None = None
    try:
        await asyncio.wait_for(archiving.wait(), 5.0)
        running = asyncio.create_task(rig.ops.run(request_for(rig.device_id, rig.root)))
        await asyncio.sleep(0.5)
        # The archive holds the sessions, so the recording waits for it.  Without
        # that, it would record beside the delete, and the cancellation that
        # follows might not see it.
        assert await operation_rows(session_factory, rig.device_id) == []
        release.set()
        await asyncio.wait_for(deleting, 5.0)
        with pytest.raises(DeviceOperationError, match="This session was stopped"):
            await asyncio.wait_for(running, 5.0)
        assert await operation_rows(session_factory, rig.device_id) == []
    finally:
        release.set()
        await asyncio.gather(deleting, return_exceptions=True)
        if running is not None:
            await stop(running)


async def test_an_artifact_is_made_in_the_folder_under_its_tool_call(laptop_rig, session_factory, redis_client):
    rig = laptop_rig
    await rig.laptop.connect()
    store = SessionStore(session_factory)
    result = await tool_call(
        rig, store, builtin_tools(), "call_1", "create_artifact",
        {"name": "notes", "kind": "markdown", "spec": {"content": "# Notes"}},
        redis_client=redis_client, session_factory=session_factory,
    )
    made = json.loads(result["content"])
    assert made["success"] is True, made
    assert (rig.folder / ".surogates-results" / "artifacts" / str(rig.root) / made["artifact_id"] / "v1.json").is_file()
    # Under the call: a resumed call finds them, and is reported interrupted.
    call_event = await call_event_of(store, rig.root, "call_1")
    async with session_factory() as db:
        invocations = set((await db.execute(
            select(DeviceOperation.invocation_id)
            .where(DeviceOperation.root_session_id == rig.root, DeviceOperation.kind != BIND)
        )).scalars())
    assert invocations == {f"{call_event}:call_1"}
    [created] = await store.get_events(rig.root, types=[EventType.ARTIFACT_CREATED])
    assert created.data["artifact_id"] == made["artifact_id"]


async def test_a_fenced_svg_is_promoted_into_the_folder(laptop_rig, session_factory, redis_client):
    rig = laptop_rig
    await rig.laptop.connect()
    store = SessionStore(session_factory)
    harness = SimpleNamespace(
        _api_client=None, _storage=None, _session_factory=session_factory, _redis=redis_client, _store=store,
    )
    await ArtifactCompletionMixin._promote_fenced_artifacts(
        harness, await store.get_session(rig.root), "Here:\n```svg\n<svg viewBox='0 0 1 1'></svg>\n```", [],
    )
    [created] = await store.get_events(rig.root, types=[EventType.ARTIFACT_CREATED])
    assert (rig.folder / ".surogates-results" / "artifacts" / str(rig.root) / created.data["artifact_id"] / "v1.json").is_file()


async def test_a_fenced_svg_waits_on_no_computer_that_is_away(laptop_rig, session_factory, redis_client):
    rig = laptop_rig
    store = SessionStore(session_factory)
    harness = SimpleNamespace(
        _api_client=None, _storage=None, _session_factory=session_factory, _redis=redis_client, _store=store,
    )
    # The laptop never connected: said at once, and the reply goes on without it.
    await asyncio.wait_for(ArtifactCompletionMixin._promote_fenced_artifacts(
        harness, await store.get_session(rig.root), "```svg\n<svg></svg>\n```", [],
    ), 5.0)
    assert await store.get_events(rig.root, types=[EventType.ARTIFACT_CREATED]) == []


async def test_a_fenced_svg_waits_no_longer_than_its_bound_on_a_computer_that_does_not_answer(
    laptop_rig, session_factory, redis_client, monkeypatch,
):
    rig = laptop_rig
    await rig.laptop.connect()
    rig.laptop.hold = True  # online, but answers nothing
    monkeypatch.setattr(loop_artifact_completion, "HARNESS_WITHIN_S", 0.5)
    store = SessionStore(session_factory)
    harness = SimpleNamespace(
        _api_client=None, _storage=None, _session_factory=session_factory, _redis=redis_client, _store=store,
    )
    await asyncio.wait_for(ArtifactCompletionMixin._promote_fenced_artifacts(
        harness, await store.get_session(rig.root), "```svg\n<svg></svg>\n```", [],
    ), 3.0)
    assert await store.get_events(rig.root, types=[EventType.ARTIFACT_CREATED]) == []


class TurnEnd(ArtifactCompletionMixin):
    """The harness's turn-end scan, with only what it reads."""

    def __init__(self, store, session_factory, redis_client) -> None:
        self._store, self._session_factory, self._redis, self._storage = store, session_factory, redis_client, None
        self._turn_started_at = datetime.now(timezone.utc)


async def test_the_turns_files_are_what_its_computer_changed_since_the_turn_began(
    laptop_rig, session_factory, redis_client,
):
    rig = laptop_rig
    (rig.folder / "before.md").write_text("old")
    # Past the cursor's margin, which covers a filesystem's coarser clock.
    await asyncio.sleep(WALK_MARGIN_NS / 1e9 + 0.1)
    await rig.laptop.connect()
    store = SessionStore(session_factory)
    session = await store.get_session(rig.root)
    turn = TurnEnd(store, session_factory, redis_client)
    # At its first tool call.
    await turn._mark_turn_start(session)
    assert turn._turn_cursor is not None
    for name, text in [("report.md", "new"), ("seen.md", "s"), ("empty.md", ""), ("uploads/in.md", "u"),
                       (".surogates-results/terminal-output-1.log", "x"), ("node_modules/x/i.js", ""),
                       # The user's own: the harness keeps a local folder's artifacts under .surogates-results/.
                       ("_artifacts/summary.md", "a")]:
        (rig.folder / name).parent.mkdir(parents=True, exist_ok=True)
        (rig.folder / name).write_text(text)
    root = session.config["workspace_path"]

    found, entries = await turn._scan_workspace_for_new_files(
        session_id=rig.root, already_seen_paths={f"{root}/seen.md"},
    )

    assert sorted(artifact.ref for artifact in found) == ["_artifacts/summary.md", "empty.md", "report.md"]
    # No stamp: the folder's clock chose them, and the server's is another.
    assert entries["report.md"] == entries[f"{root}/report.md"] == {"size": 3, "modified": None}
    assert entries["empty.md"]["size"] == 0
    assert "before.md" not in entries


class Listing:
    """The cloud's storage, recording what a turn lists of it."""

    def __init__(self) -> None:
        self.listed: list[tuple[str, str]] = []

    async def list_entries(self, bucket, prefix=""):
        self.listed.append((bucket, prefix))
        return [{"key": f"{prefix}report.md", "size": 3, "modified": datetime.now(timezone.utc)}]


async def test_a_turn_whose_computer_was_away_at_its_start_lists_no_files_and_never_the_cloud(
    laptop_rig, session_factory, redis_client, caplog,
):
    rig = laptop_rig
    caplog.set_level(logging.INFO, logger="surogates.harness.loop_artifact_completion")
    store = SessionStore(session_factory)
    turn = TurnEnd(store, session_factory, redis_client)
    turn._storage = listing = Listing()
    # The laptop never connected: no cursor at its first tool call, and nothing waited on.
    await asyncio.wait_for(turn._mark_turn_start(await store.get_session(rig.root)), 5.0)
    assert turn._turn_cursor is None

    found, entries = await turn._scan_workspace_for_new_files(session_id=rig.root, already_seen_paths=set())

    # The chat's cloud prefix is metadata only: no permission to read it.  Nothing was seen.
    assert (found, entries, listing.listed) == ([], None, [])
    assert "did not say where this turn began" in caplog.text


async def test_a_computer_that_does_not_answer_holds_the_turn_no_longer_than_its_bound(
    laptop_rig, session_factory, redis_client, monkeypatch,
):
    rig = laptop_rig
    await rig.laptop.connect()
    rig.laptop.hold = True  # online, but answers nothing
    monkeypatch.setattr(loop_artifact_completion, "HARNESS_WITHIN_S", 0.5)
    store = SessionStore(session_factory)
    session = await store.get_session(rig.root)
    turn = TurnEnd(store, session_factory, redis_client)
    assert await asyncio.wait_for(turn._folder_cursor(session), 3.0) is None
    assert await asyncio.wait_for(turn._walk_folder(session, since="1"), 3.0) is None


class Prompting(ContextReplayMixin):
    """The harness's prompt build, with only what it reads."""

    class Builder:
        folder_context: str | None = None

        def build(self) -> str:
            return f"prompt with {self.folder_context}"

    def __init__(self, session_factory, redis_client) -> None:
        self._session_factory, self._redis, self._storage = session_factory, redis_client, None
        self._system_prompt_cache, self._prompt = SystemPromptCache(), self.Builder()
        self._coding_repos, self._ssh_targets = [], []


async def test_a_local_folders_agents_md_is_read_through_its_computer_when_the_prompt_is_built(
    laptop_rig, session_factory, redis_client,
):
    rig = laptop_rig
    (rig.folder / "AGENTS.md").write_text("Use tabs.")
    await rig.laptop.connect()
    session = await SessionStore(session_factory).get_session(rig.root)
    harness = Prompting(session_factory, redis_client)

    assert await harness._build_system_prompt(session) == "prompt with Use tabs."
    looked = len(rig.laptop.ran)
    # Built once, as any prompt: the next wake on this worker reuses it.
    assert await harness._build_system_prompt(session) == "prompt with Use tabs."
    assert len(rig.laptop.ran) == looked


async def test_a_prompt_built_while_its_computer_is_away_goes_without_the_folders_context_and_is_not_kept(
    laptop_rig, session_factory, redis_client,
):
    rig = laptop_rig
    (rig.folder / "AGENTS.md").write_text("Use tabs.")
    session = await SessionStore(session_factory).get_session(rig.root)
    harness = Prompting(session_factory, redis_client)
    assert await asyncio.wait_for(harness._build_system_prompt(session), 5.0) == "prompt with None"
    # Built again once the computer is back, rather than cached without it.
    await rig.laptop.connect()
    assert await harness._build_system_prompt(session) == "prompt with Use tabs."


async def test_a_prompt_waits_no_longer_than_its_bound_on_a_computer_that_does_not_answer(
    laptop_rig, session_factory, redis_client, monkeypatch,
):
    rig = laptop_rig
    await rig.laptop.connect()
    rig.laptop.hold = True  # online, but answers nothing
    monkeypatch.setattr(loop_context_replay, "HARNESS_WITHIN_S", 0.5)
    session = await SessionStore(session_factory).get_session(rig.root)
    prompt = await asyncio.wait_for(Prompting(session_factory, redis_client)._build_system_prompt(session), 3.0)
    assert prompt == "prompt with None"
