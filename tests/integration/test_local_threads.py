"""A project's threads that work in a folder on the user's computer."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from surogates.devices.binding import Binding
from surogates.devices.operations import DeviceOperations
from surogates.devices.store import DeviceStore
from surogates.session.events import EventType
from surogates.session.store import SessionStore

from .test_devices import (  # noqa: F401  (api is a fixture)
    AGENT_ID,
    FOLDER,
    NONCE,
    add_user,
    api,
    binding,
    register,
)
from .test_workstream_threads import PROPOSED, call_tool, children_of, events_of, queued, start
from .test_workstreams import create, master_of

pytestmark = pytest.mark.asyncio(loop_scope="session")


def journal(api) -> DeviceOperations:
    return DeviceOperations(api.app.state.session_factory, api.app.state.redis)


async def own_root(api, device_id: UUID) -> UUID:
    """A session that is its own sandbox root, as a project's thread is, on *device_id*'s folder, not bound yet."""
    session_id = uuid4()
    await SessionStore(api.app.state.session_factory).create_session(
        session_id=session_id, user_id=api.user_id, org_id=api.org_id, agent_id=AGENT_ID, channel="worker",
        config={
            "execution": {"kind": "device", "device_id": str(device_id), "device_name": "Flavius's ThinkPad"},
            "workspace_path": FOLDER,
            "storage_bucket": "test-bucket",
            "storage_key_prefix": "",
            "sandbox_root_session_id": str(session_id),
        },
    )
    return session_id


async def bound_own_root(api) -> tuple[UUID, UUID]:
    device_id = UUID((await register(api))["id"])
    root = await own_root(api, device_id)
    ops = journal(api)
    await ops.bind(session_id=root, device_id=device_id, folder=FOLDER, nonce=NONCE)
    [bind] = await ops.pending(device_id, 1)
    assert await ops.complete(device_id, 1, bind.id, bind.digest, {"ok": None}) == "completed"
    return device_id, root


async def test_a_session_that_is_its_own_sandbox_root_is_bound_to_a_folder(api):
    _, root = await bound_own_root(api)
    assert await binding(api, root) == Binding("bound")


async def test_deleting_a_session_that_is_its_own_sandbox_root_has_its_computer_forget_the_folder(api):
    device_id, root = await bound_own_root(api)
    deleted = await api.client.delete(f"/v1/sessions/{root}", headers=api.auth())
    assert deleted.status_code == 204, deleted.text
    [retire] = await journal(api).pending(device_id, 1)
    assert (retire.kind, retire.root_session_id, retire.args) == ("retire", root, {})


async def device_card(api) -> tuple[dict, object, str]:
    """A project whose master proposed ``PROPOSED``: card "2" is a thread on the user's computer."""
    project = await create(api)
    master = await master_of(api, project)
    return project, master, (await call_tool(api, master, "propose_threads", threads=PROPOSED))["proposal_id"]


def confirmed(device_id: str, **changes) -> dict:
    """What the page sends for a folder its user confirmed on *device_id*."""
    return {"kind": "device", "device_id": device_id, "folder": FOLDER, "nonce": NONCE, **changes}


async def make_local(api, project: dict, proposal_id: str, execution: dict, *, key: str = "2", token: str | None = None):
    return await api.client.post(
        f"/v1/workstreams/{project['id']}/threads",
        json={"proposal_id": proposal_id, "key": key, "execution": execution}, headers=api.auth(token),
    )


async def test_a_card_makes_its_thread_in_the_folder_the_user_confirmed_waiting_for_its_binding(api):
    device = await register(api)
    project, master, proposal_id = await device_card(api)
    response = await make_local(api, project, proposal_id, confirmed(device["id"]))
    assert response.status_code == 201, response.text
    thread = await api.app.state.session_store.get_session(UUID(response.json()["thread_id"]))
    # A child of the master for its report, and its own root for its computer.
    assert (thread.parent_id, thread.user_id) == (master.id, master.user_id)
    config = thread.config
    assert config["execution"] == {"kind": "device", "device_id": device["id"], "device_name": "Flavius's ThinkPad"}
    assert (config["workspace_path"], config["sandbox_root_session_id"]) == (FOLDER, str(thread.id))
    assert (config["workstream_role"], config["memory_boundary"]) == ("thread", master.config["memory_boundary"])
    assert config["workstream_card"] == {"proposal_id": proposal_id, "key": "2"}
    assert await binding(api, thread.id) == Binding("pending")
    [bind] = await journal(api).pending(UUID(device["id"]), 1)
    assert (bind.kind, bind.root_session_id, bind.args) == ("bind", thread.id, {"folder": FOLDER, "nonce": NONCE})
    # Not started: it has no goal, the master has not heard of it, nothing runs it, and no row lists it.
    assert await events_of(api, thread.id) == []
    assert await events_of(api, master.id, EventType.WORKER_SPAWNED) == []
    assert not await queued(api, thread)
    listed = await api.client.get(f"/v1/workstreams/{project['id']}/threads", headers=api.auth())
    assert listed.json() == []


async def test_a_card_allowed_twice_at_once_makes_one_thread(api):
    device = await register(api)
    project, master, proposal_id = await device_card(api)
    both = await asyncio.gather(*(make_local(api, project, proposal_id, confirmed(device["id"])) for _ in range(2)))
    assert sorted(response.status_code for response in both) == [201, 409]
    assert len(await children_of(api, master)) == 1


async def test_a_thread_works_only_on_the_users_own_live_computer(api, session_factory):
    project, master, proposal_id = await device_card(api)
    _, their_token = await add_user(session_factory, api.org_id)
    theirs = await register(api, token=their_token)
    elsewhere = await DeviceStore(session_factory).create(
        org_id=api.org_id, agent_id="another-agent", user_id=api.user_id, name="Elsewhere",
    )
    revoked = await register(api)
    await api.client.delete(f"/v1/devices/{revoked['id']}", headers=api.auth())
    mine = await register(api)
    for case, (execution, status) in {
        "another user's computer": (confirmed(theirs["id"]), 404),
        "another agent's computer": (confirmed(str(elsewhere.device.id)), 404),
        "a revoked computer": (confirmed(revoked["id"]), 409),
        "a nonce that is no nonce": (confirmed(mine["id"], nonce="short"), 422),
        "a folder with a NUL": (confirmed(mine["id"], folder="/home/fl\x00avius"), 422),
    }.items():
        response = await make_local(api, project, proposal_id, execution)
        assert response.status_code == status, case
    assert (await make_local(api, project, proposal_id, confirmed(mine["id"]), key="3")).status_code == 404
    # Nothing was made under the master, and no computer was asked for a folder.
    assert await children_of(api, master) == []
    for device_id in (theirs["id"], mine["id"]):
        assert await journal(api).pending(UUID(device_id), 1) == []


async def test_a_chat_on_the_users_computer_cannot_pass_for_a_thread(api):
    device = await register(api)
    master = await master_of(api, await create(api))
    forged = {
        "workstream_id": master.config["workstream_id"], "workstream_role": "thread",
        "workstream_card": {"proposal_id": str(uuid4()), "key": "2"}, "sandbox_root_session_id": str(uuid4()),
    }
    response = await api.client.post("/v1/sessions", json={"execution": confirmed(device["id"]), "config": forged}, headers=api.auth())
    assert response.status_code == 201, response.text
    chat = await api.app.state.session_store.get_session(UUID(response.json()["id"]))
    assert chat.parent_id is None
    assert set(forged) & set(chat.config) == set()


async def test_the_coordinator_cannot_make_a_thread_on_the_users_computer(api):
    device = await register(api)
    master = await master_of(api, await create(api))
    thread = await start(api, master, execution=confirmed(device["id"]))
    assert "execution" not in thread.config
    assert await journal(api).pending(UUID(device["id"]), 1) == []
