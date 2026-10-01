"""Chats that work on a folder of the user's computer."""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from surogates.db.models import Session
from surogates.devices.binding import Binding, binding_of
from surogates.devices.operations import DeviceOperations
from surogates.devices.presence import DevicePresence
from surogates.devices.store import DeviceStore
from surogates.runtime import agent_runtime_context_dep, build_agent_runtime_context
from surogates.tools.workspace_io import LocalWorkspaceIO
from tests.fake_laptop import FakeLaptop

from .conftest import issue_service_account_token
from .test_devices import (
    AGENT_ID,
    FOLDER,
    NONCE,
    add_user,
    api,  # noqa: F401  (a fixture)
    eventually,
    link_url,  # noqa: F401  (a fixture)
    next_control,
    register,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


def local(device_id: str, **changes) -> dict:
    """A create request for a chat on *device_id*'s folder."""
    return {"execution": {"kind": "device", "device_id": device_id, "folder": FOLDER, "nonce": NONCE, **changes}}


async def local_chat(api, device_id: str, **changes) -> str:
    response = await api.client.post("/v1/sessions", json=local(device_id, **changes), headers=api.auth())
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def binding(api, session_id: str) -> Binding:
    async with api.app.state.session_factory() as db:
        return await binding_of(db, UUID(session_id))


def journal(api) -> DeviceOperations:
    return DeviceOperations(api.app.state.session_factory, api.app.state.redis)


async def test_a_local_folder_chat_is_created_waiting_for_its_binding(api):
    device = await register(api)
    response = await api.client.post("/v1/sessions", json=local(device["id"]), headers=api.auth())
    assert response.status_code == 201, response.text
    session = response.json()
    assert session["config"]["execution"] == {"kind": "device", "device_id": device["id"]}
    assert session["config"]["workspace_path"] == FOLDER
    assert await binding(api, session["id"]) == Binding("pending")
    [op] = await journal(api).pending(UUID(device["id"]), 1)
    assert (op.kind, op.root_session_id, op.args) == (
        "bind", UUID(session["id"]), {"folder": FOLDER, "nonce": NONCE},
    )


async def test_the_computer_is_told_about_a_new_chat(api, redis_client):
    device = await register(api)
    control = await DevicePresence(redis_client).subscribe(UUID(device["id"]))
    try:
        await local_chat(api, device["id"])
        assert (await next_control(control)).startswith("op:")
    finally:
        await control.aclose()


@pytest.mark.parametrize(
    "folder",
    ["/home/flavius/Documente/Lucrări — 2026/日本語", "/" + "a" * 4095],
    ids=["non-ascii", "max-length"],
)
async def test_a_folder_name_is_kept_intact(api, folder):
    device = await register(api)
    session_id = await local_chat(api, device["id"], folder=folder)
    [op] = await journal(api).pending(UUID(device["id"]), 1)
    assert op.args["folder"] == folder
    response = await api.client.get(f"/v1/sessions/{session_id}", headers=api.auth())
    assert response.json()["config"]["workspace_path"] == folder


async def test_a_chat_cannot_use_another_users_device(api, session_factory):
    _, their_token = await add_user(session_factory, api.org_id)
    theirs = await register(api, token=their_token)
    response = await api.client.post("/v1/sessions", json=local(theirs["id"]), headers=api.auth())
    assert response.status_code == 404, response.text
    assert await journal(api).pending(UUID(theirs["id"]), 1) == []


async def test_a_chat_cannot_use_a_device_of_another_agent(api, session_factory):
    issued = await DeviceStore(session_factory).create(
        org_id=api.org_id, agent_id="another-agent", user_id=api.user_id, name="Elsewhere",
    )
    response = await api.client.post("/v1/sessions", json=local(str(issued.device.id)), headers=api.auth())
    assert response.status_code == 404, response.text


async def test_a_chat_cannot_use_a_revoked_device(api):
    device = await register(api)
    await api.client.delete(f"/v1/devices/{device['id']}", headers=api.auth())
    response = await api.client.post("/v1/sessions", json=local(device["id"]), headers=api.auth())
    assert response.status_code == 409, response.text
    assert "revoked" in response.json()["detail"]


@pytest.mark.parametrize("changes", [
    {"kind": "cloud"},
    {"nonce": None},
    {"nonce": "short"},
    {"nonce": "not a nonce at all!"},
    {"folder": ""},
    {"folder": "/" + "a" * 4096},
    {"folder": "/home/fl\x00avius"},
], ids=["kind", "no-nonce", "short-nonce", "bad-nonce", "empty-folder", "long-folder", "nul-folder"])
async def test_a_malformed_local_folder_request_is_refused(api, changes):
    device = await register(api)
    response = await api.client.post("/v1/sessions", json=local(device["id"], **changes), headers=api.auth())
    assert response.status_code == 422, response.text
    assert await journal(api).pending(UUID(device["id"]), 1) == []


async def session_count(api) -> int:
    async with api.app.state.session_factory() as db:
        return await db.scalar(select(func.count()).select_from(Session).where(Session.user_id == api.user_id))


async def test_a_folder_that_is_not_valid_unicode_creates_nothing(api):
    device = await register(api)
    sessions_before = await session_count(api)
    # FastAPI refuses the body, then cannot render its own 422 (the error
    # echoes the input) and fails with 500: the status is not ours to choose,
    # creating nothing is.  A client of its own, so that failure comes back as
    # a response instead of being raised into the test.
    async with AsyncClient(
        transport=ASGITransport(app=api.app, raise_app_exceptions=False), base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/sessions",
            # Encoded here, so the lone surrogate travels as the \ud800 escape a client would send.
            content=json.dumps(local(device["id"], folder="/home/\ud800")),
            headers={**api.auth(), "Content-Type": "application/json"},
        )
    assert response.status_code != 201, response.text
    assert await journal(api).pending(UUID(device["id"]), 1) == []
    assert await session_count(api) == sessions_before


async def test_only_a_signed_in_users_chat_can_use_a_local_folder(api, session_factory):
    device = await register(api)
    account = await issue_service_account_token(session_factory, api.org_id)
    response = await api.client.post(
        "/v1/api/sessions", json=local(device["id"]),
        headers={"Authorization": f"Bearer {account.token}"},
    )
    assert response.status_code == 400, response.text


async def test_an_agent_with_one_conversation_refuses_local_folders(api):
    api.app.dependency_overrides[agent_runtime_context_dep] = lambda: build_agent_runtime_context({
        "agent_id": AGENT_ID,
        "org_id": str(api.org_id),
        "project_id": "test-project",
        "enabled": True,
        "version": 1,
        "storage_key_prefix": "",
        "multi_session": False,
    })
    device = await register(api)
    response = await api.client.post("/v1/sessions", json=local(device["id"]), headers=api.auth())
    assert response.status_code == 409, response.text


async def test_config_cannot_make_a_chat_local(api):
    device = await register(api)
    response = await api.client.post(
        "/v1/sessions",
        json={"config": {
            "execution": {"kind": "device", "device_id": device["id"]},
            "workspace_path": "/etc",
        }},
        headers=api.auth(),
    )
    assert response.status_code == 201, response.text
    config = response.json()["config"]
    assert "execution" not in config
    assert config["workspace_path"] != "/etc"
    assert await journal(api).pending(UUID(device["id"]), 1) == []


async def test_the_server_offers_local_folder_chats(api):
    response = await api.client.get("/v1/auth/config")
    assert response.status_code == 200, response.text
    assert response.json()["desktop_sessions"] is True


async def is_bound(api, session_id: str) -> bool:
    return (await binding(api, session_id)).state == "bound"


async def has_failed(api, session_id: str) -> bool:
    return (await binding(api, session_id)).state == "failed"


async def send(api, session_id: str):
    return await api.client.post(
        f"/v1/sessions/{session_id}/messages", json={"content": "Tidy up my notes"}, headers=api.auth(),
    )


async def test_messages_wait_for_the_computer_to_accept_the_folder(api, link_url, tmp_path):
    device = await register(api)
    session_id = await local_chat(api, device["id"])
    refused = await send(api, session_id)
    assert refused.status_code == 409, refused.text
    assert "still being set up" in refused.json()["detail"]

    # The app was offline when the chat was created; it binds the chat when it connects.
    laptop = FakeLaptop(link_url, device["token"], LocalWorkspaceIO(str(tmp_path)))
    laptop.prepare(NONCE, FOLDER)
    await laptop.connect()
    try:
        await eventually(lambda: is_bound(api, session_id))
        assert laptop.bindings == {session_id: FOLDER}
        accepted = await send(api, session_id)
        assert accepted.status_code == 202, accepted.text
    finally:
        await laptop.disconnect()


async def test_a_folder_the_user_did_not_confirm_fails_the_chat(api, link_url, tmp_path):
    device = await register(api)
    session_id = await local_chat(api, device["id"])
    laptop = FakeLaptop(link_url, device["token"], LocalWorkspaceIO(str(tmp_path)))
    await laptop.connect()  # nothing prepared: the user never confirmed this folder
    try:
        await eventually(lambda: has_failed(api, session_id))
        refused = await send(api, session_id)
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"] == (
            "This chat's folder could not be set up: "
            "This folder was not confirmed on this computer. Start a new chat."
        )
    finally:
        await laptop.disconnect()


async def test_uploads_wait_for_the_binding(api):
    device = await register(api)
    session_id = await local_chat(api, device["id"])
    response = await api.client.post(
        f"/v1/sessions/{session_id}/workspace/upload",
        files={"file": ("notes.txt", b"draft")},
        headers=api.auth(),
    )
    assert response.status_code == 409, response.text


async def test_a_defined_outcome_waits_for_the_binding(api):
    # It writes a user message and wakes the worker, like a message does.
    device = await register(api)
    session_id = await local_chat(api, device["id"])
    response = await api.client.post(
        f"/v1/sessions/{session_id}/events",
        json={"events": [{
            "type": "user.define_outcome",
            "description": "Tidy up my notes",
            "rubric": {"type": "text", "content": "- every note has a title"},
            "max_iterations": 5,
        }]},
        headers=api.auth(),
    )
    assert response.status_code == 409, response.text


async def test_a_cloud_chat_takes_messages_at_once(api):
    created = await api.client.post("/v1/sessions", json={}, headers=api.auth())
    assert created.status_code == 201, created.text
    accepted = await send(api, created.json()["id"])
    assert accepted.status_code == 202, accepted.text


async def paused_local_chat(api) -> str:
    device = await register(api)
    session_id = await local_chat(api, device["id"])
    paused = await api.client.post(f"/v1/sessions/{session_id}/pause", headers=api.auth())
    assert paused.status_code == 200, paused.text
    return session_id


# Resuming and retrying emit a resume event and wake the worker, like a message does.
async def test_resuming_a_chat_waits_for_the_binding(api):
    session_id = await paused_local_chat(api)
    response = await api.client.post(f"/v1/sessions/{session_id}/resume", headers=api.auth())
    assert response.status_code == 409, response.text
    assert "still being set up" in response.json()["detail"]


async def test_retrying_a_chat_waits_for_the_binding(api):
    session_id = await paused_local_chat(api)
    response = await api.client.post(f"/v1/sessions/{session_id}/retry", headers=api.auth())
    assert response.status_code == 409, response.text
    assert "still being set up" in response.json()["detail"]


async def test_the_orphan_sweep_leaves_a_waiting_local_chat_alone(api):
    device = await register(api)
    local_id = await local_chat(api, device["id"])
    cloud = await api.client.post("/v1/sessions", json={}, headers=api.auth())
    assert cloud.status_code == 201, cloud.text
    orphans = await api.app.state.session_store.find_orphaned_sessions(
        stale_seconds=0, agent_id=AGENT_ID, limit=10000,
    )
    found = {str(s.id) for s in orphans}
    assert local_id not in found
    assert cloud.json()["id"] in found
