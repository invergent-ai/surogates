"""A project's threads that work in a folder on the user's computer."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

import surogates.devices.operations as operations_module
from surogates.devices.binding import Binding
from surogates.devices.operations import DeviceOperations
from surogates.devices.store import DeviceStore
from surogates.harness.tool_exec import execute_single_tool
from surogates.session.events import EventType
from surogates.session.store import SessionStore
from surogates.tools.workspace_io import LocalWorkspaceIO
from tests.fake_laptop import FakeLaptop

from .test_device_sessions import has_failed, is_bound
from .test_devices import (  # noqa: F401  (api and link_url are fixtures)
    AGENT_ID,
    FOLDER,
    NONCE,
    add_user,
    api,
    binding,
    builtin_tools,
    eventually,
    link_url,
    register,
)
from .test_workstream_library import library, upload
from .test_workstream_overview import rows
from .test_workstream_threads import PROPOSED, call_tool, children_of, events_of, queued, start, turn_ends
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


@pytest_asyncio.fixture(loop_scope="session")
async def laptop(api, link_url, tmp_path):
    """A registered computer's fake app, where the user confirmed ``FOLDER`` under ``NONCE``, not connected yet."""
    device = await register(api)
    folder = (tmp_path / "laptop").resolve()
    folder.mkdir()
    app = FakeLaptop(link_url, device["token"], LocalWorkspaceIO(str(folder)))
    app.prepare(NONCE, FOLDER)
    yield SimpleNamespace(app=app, device_id=device["id"], folder=folder)
    await app.disconnect()


async def begin(api, project: dict, thread_id: str, token: str | None = None):
    return await api.client.post(
        f"/v1/workstreams/{project['id']}/threads/{thread_id}/start", headers=api.auth(token),
    )


async def made_local(api, laptop) -> tuple[dict, object, str, str]:
    """A project, its master, the proposal and the thread made from its card "2" on *laptop*'s computer."""
    project, master, proposal_id = await device_card(api)
    made = await make_local(api, project, proposal_id, confirmed(laptop.device_id))
    assert made.status_code == 201, made.text
    return project, master, proposal_id, made.json()["thread_id"]


async def begun_local(api, laptop):
    """A project, its master and a thread begun on *laptop*'s computer, which is connected."""
    project, master, _, thread_id = await made_local(api, laptop)
    await laptop.app.connect()
    await eventually(lambda: is_bound(api, thread_id))
    assert (await begin(api, project, thread_id)).status_code == 201
    return project, master, await api.app.state.session_store.get_session(UUID(thread_id))


async def test_a_thread_on_the_users_computer_begins_once_its_computer_has_bound_it(api, laptop):
    project, master, proposal_id, thread_id = await made_local(api, laptop)
    early = await begin(api, project, thread_id)
    assert (early.status_code, early.json()["detail"]) == (409, "This chat's folder is still being set up on your computer.")
    assert await events_of(api, UUID(thread_id)) == []

    await laptop.app.connect()
    await eventually(lambda: is_bound(api, thread_id))
    assert laptop.app.bindings == {thread_id: FOLDER}
    response = await begin(api, project, thread_id)
    assert response.status_code == 201, response.text
    assert (response.json()["id"], response.json()["title"], response.json()["group"]) == (thread_id, "Check the totals", "working")
    thread = await api.app.state.session_store.get_session(UUID(thread_id))
    [goal] = await events_of(api, thread.id, EventType.USER_MESSAGE)
    assert goal.data == {"content": "Check the totals in Budget.xlsx."}
    [spawned] = await events_of(api, master.id, EventType.WORKER_SPAWNED)
    assert spawned.data == {
        "worker_id": thread_id, "title": "Check the totals", "goal": "Check the totals in Budget.xlsx.",
        "started_by": "user", "proposal_id": proposal_id, "key": "2",
    }
    assert await queued(api, thread)


async def test_a_thread_begins_once(api, laptop):
    project, master, _, thread_id = await made_local(api, laptop)
    await laptop.app.connect()
    await eventually(lambda: is_bound(api, thread_id))
    both = await asyncio.gather(*(begin(api, project, thread_id) for _ in range(2)))
    assert sorted(response.status_code for response in both) == [201, 409]
    again = await begin(api, project, thread_id)
    assert (again.status_code, again.json()["detail"]) == (409, "This thread was already started.")
    assert len(await events_of(api, master.id, EventType.WORKER_SPAWNED)) == 1
    assert len(await events_of(api, UUID(thread_id), EventType.USER_MESSAGE)) == 1


async def test_a_folder_the_user_did_not_confirm_keeps_its_thread_from_beginning(api, laptop):
    laptop.app.prepared.clear()
    project, master, _, thread_id = await made_local(api, laptop)
    await laptop.app.connect()
    await eventually(lambda: has_failed(api, thread_id))
    refused = await begin(api, project, thread_id)
    assert (refused.status_code, refused.json()["detail"]) == (409, (
        "This chat's folder could not be set up: This folder was not confirmed on this computer. Start a new chat."
    ))
    assert await events_of(api, master.id, EventType.WORKER_SPAWNED) == []


async def test_only_the_projects_own_threads_made_on_a_computer_begin(api, laptop, session_factory):
    project, master, _, thread_id = await made_local(api, laptop)
    await laptop.app.connect()
    await eventually(lambda: is_bound(api, thread_id))
    other = await create(api, name="Budget")
    cloud = await start(api, master)
    _, their_token = await add_user(session_factory, api.org_id)
    for case, (asked, thread, token, detail) in {
        "another project": (other, thread_id, None, "No such thread."),
        "an unknown thread": (project, str(uuid4()), None, "No such thread."),
        "the master": (project, str(master.id), None, "No such thread."),
        "a thread in the cloud": (project, str(cloud.id), None, "No such thread."),
        "another user": (project, thread_id, their_token, "No such project."),
    }.items():
        refused = await begin(api, asked, thread, token)
        assert (refused.status_code, refused.json()["detail"]) == (404, detail), case
    assert await events_of(api, UUID(thread_id)) == []


async def test_a_thread_proposed_for_the_users_computer_runs_in_the_cloud_when_the_user_says_so(api, laptop):
    project, master, proposal_id = await device_card(api)
    response = await api.client.post(
        f"/v1/workstreams/{project['id']}/threads", json={"proposal_id": proposal_id, "key": "2"}, headers=api.auth(),
    )
    assert response.status_code == 201, response.text
    thread = await api.app.state.session_store.get_session(UUID(response.json()["id"]))
    assert "execution" not in thread.config
    assert await queued(api, thread)
    # Started in the cloud, it is not made again on the computer.
    again = await make_local(api, project, proposal_id, confirmed(laptop.device_id))
    assert (again.status_code, again.json()["detail"]) == (409, "This thread was already started.")
    assert await journal(api).pending(UUID(laptop.device_id), 1) == []


PLACE = {"kind": "device", "device_name": "Flavius's ThinkPad"}


async def test_a_threads_row_names_its_computer_and_says_when_its_work_waits_for_it(api, laptop, monkeypatch):
    monkeypatch.setattr(operations_module, "WAIT_GRACE_S", 0.1)
    project, _, thread = await begun_local(api, laptop)
    here = {**PLACE, "device_id": laptop.device_id}
    [row] = await rows(api, project)
    assert row["place"] == {**here, "online": True}
    [one] = await rows(api, project, thread_id=str(thread.id))
    assert one["place"] == {**here, "online": True}

    # The network goes while the thread works: its step waits for the computer, and its row says so.
    await laptop.app.disconnect()

    async def offline() -> bool:
        return (await rows(api, project))[0]["place"] == {**here, "online": False}

    await eventually(offline)
    store = api.app.state.session_store
    step = asyncio.create_task(execute_single_tool(
        {"id": "call_1", "function": {"name": "write_file", "arguments": json.dumps({"path": "Totals.md", "content": "42\n"})}},
        session=thread, lease=await store.try_acquire_lease(thread.id, "worker-local", ttl_seconds=60), store=store,
        tools=builtin_tools(), tenant=MagicMock(asset_root="/tmp/test"),
        redis=api.app.state.redis, session_factory=api.app.state.session_factory,
    ))

    async def waiting() -> bool:
        [row] = await rows(api, project)
        return (row["group"], row["reason"], row["status_line"]) == ("working", "computer", "Waiting for Flavius's ThinkPad")

    await eventually(waiting)
    await laptop.app.connect()
    await asyncio.wait_for(step, 10.0)
    [row] = await rows(api, project)
    assert (row["reason"], row["place"]["online"]) == (None, True)
    assert (laptop.folder / "Totals.md").read_text() == "42\n"


async def test_the_library_lists_a_local_threads_files_on_its_computer_and_the_clouds_as_they_are(api, laptop):
    project, master, thread = await begun_local(api, laptop)
    await upload(api, master, "Budget.xlsx", b"PK the cloud's")
    # A ref as the model gave it: from the folder's top, or the whole path there. One outside the folder is none of its files.
    await turn_ends(api, thread, files=[
        "Budget.xlsx", f"{FOLDER}/Totals.md", "_artifacts/Report.pdf", "../Elsewhere/secret.txt", "/etc/hosts",
    ])
    response = await library(api, project)
    assert response.status_code == 200, response.text
    here = {**PLACE, "device_id": laptop.device_id, "online": True}
    listed = [(e["path"], e["origin"], e["thread_id"], e["size"], e["place"]) for e in response.json()]
    assert sorted(listed, key=repr) == sorted([
        # The cloud's file of that name is the user's: the thread made its own on the computer.
        ("Budget.xlsx", "added", None, 14, {"kind": "cloud"}),
        ("Budget.xlsx", "produced", str(thread.id), None, here),
        ("Totals.md", "produced", str(thread.id), None, here),
        # A folder's own _artifacts/ is the user's: the harness keeps its own out of it.
        ("_artifacts/Report.pdf", "produced", str(thread.id), None, here),
    ], key=repr)
    assert all(e["updated_at"].endswith("Z") for e in response.json())


async def answered_by_the_journal(api, project: dict, master) -> object:
    """A thread begun from a new card of *master* on a new computer of the user's, its binding answered as its app answers."""
    device = await register(api)
    proposal_id = (await call_tool(api, master, "propose_threads", threads=PROPOSED))["proposal_id"]
    thread_id = (await make_local(api, project, proposal_id, confirmed(device["id"]))).json()["thread_id"]
    ops = journal(api)
    [bind] = await ops.pending(UUID(device["id"]), 1)
    assert await ops.complete(UUID(device["id"]), 1, bind.id, bind.digest, {"ok": None}) == "completed"
    assert (await begin(api, project, thread_id)).status_code == 201
    return await api.app.state.session_store.get_session(UUID(thread_id))
