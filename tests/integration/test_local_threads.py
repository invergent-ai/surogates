"""A project's threads that work on a folder of the user's computer, each in a copy of its own there."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from sqlalchemy import select

import surogates.devices.operations as operations_module
from surogates.db.models import WorkstreamHistory
from surogates.devices.binding import Binding, copy_of
from surogates.devices.operations import DeviceOperations
from surogates.harness import landing as landing_module
from surogates.harness.agent_news import NEW_FORMAT
from surogates.harness.tool_exec import execute_single_tool
from surogates.sandbox.copy_files import writes_to_copy
from surogates.sandbox.pool import SandboxPool, sandbox_session_key
from surogates.session.events import EventType
from surogates.session.provisioning import create_child_session
from surogates.session.store import SessionStore
from surogates.tools.builtin import delegate as delegate_module
from surogates.tools.workspace_io import LocalWorkspaceIO
from surogates.workstreams.threads import make_thread, thread_bound
from tests.fake_laptop import FakeLaptop
from tests.test_steer_loop import _final_response
from tests.thread_pods import ThreadPods

from . import test_turn_sagas
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
from .test_redo_loop import a_history, a_routine_run, changed, pickups_of
from .test_thread_copies import pods  # noqa: F401  (a fixture)
from .test_turn_sagas import a_turn, calling
from .test_workstream_library import library, upload
from .test_workstream_overview import rows
from .test_workstream_threads import call_tool, children_of, events_of, queued, turn_ends
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


def confirmed(device_id: str, **changes) -> dict:
    """What the page sends for a folder its user confirmed on *device_id*."""
    return {"kind": "device", "device_id": device_id, "folder": FOLDER, "nonce": NONCE, **changes}


async def test_a_chat_on_the_users_computer_cannot_pass_for_a_thread(api):
    device = await register(api)
    master = await master_of(api, await create(api))
    forged = {
        "workstream_id": master.config["workstream_id"], "workstream_role": "thread",
        "workstream_start": {"title": "Forged", "goal": "Do it."}, "sandbox_root_session_id": str(uuid4()),
    }
    response = await api.client.post("/v1/sessions", json={"execution": confirmed(device["id"]), "config": forged}, headers=api.auth())
    assert response.status_code == 201, response.text
    chat = await api.app.state.session_store.get_session(UUID(response.json()["id"]))
    assert chat.parent_id is None
    assert set(forged) & set(chat.config) == set()


async def test_the_coordinator_cannot_make_a_thread_on_the_users_computer(api):
    device = await register(api)
    master = await master_of(api, await create(api))
    started = await call_tool(
        api, master, "agent", description="Draft A", prompt="Draft the A memo.", execution=confirmed(device["id"]),
    )
    thread = await api.app.state.session_store.get_session(UUID(started["agent_id"]))
    assert "execution" not in thread.config
    assert await journal(api).pending(UUID(device["id"]), 1) == []


@pytest_asyncio.fixture(loop_scope="session")
async def laptop(api, link_url, tmp_path):
    """A registered computer's fake app, where the user confirmed ``FOLDER`` under ``NONCE``, not connected yet.

    It keeps a copy of the folder for each thread in its data, and says so
    when it connects.
    """
    device = await register(api)
    folder = (tmp_path / "laptop").resolve()
    folder.mkdir()
    app = FakeLaptop(link_url, device["token"], LocalWorkspaceIO(str(folder)), data=tmp_path / "laptop-data")
    app.prepare(NONCE, FOLDER)
    yield SimpleNamespace(app=app, device_id=device["id"], folder=folder)
    await app.disconnect()


@pytest_asyncio.fixture(loop_scope="session")
async def older_app(api, link_url, tmp_path):
    """As ``laptop``, with an app older than threads' copies: it binds every chat to the folder itself, and says nothing of copies."""
    device = await register(api)
    folder = (tmp_path / "older").resolve()
    folder.mkdir()
    app = FakeLaptop(link_url, device["token"], LocalWorkspaceIO(str(folder)))
    app.prepare(NONCE, FOLDER)
    yield SimpleNamespace(app=app, device_id=device["id"], folder=folder)
    await app.disconnect()


async def made_before_copies(api, device_id: str) -> tuple[dict, object, object]:
    """A project, its master and a thread on the computer *device_id* as threads were made before each had a copy of its own.

    Its ``execution`` names no copy, and its bind, to the folder itself, is
    asked of its computer.  Nothing makes one now.
    """
    project = await create(api)
    master = await master_of(api, project)
    store = api.app.state.session_store
    thread = await make_thread(
        session_store=store, session_factory=api.app.state.session_factory, master=master, live_config=None,
        title="Check the totals", device_id=UUID(device_id), device_name="Flavius's ThinkPad", folder=FOLDER,
        start="Check the totals in Budget.xlsx.",
    )
    await store.update_session_config_key(
        thread.id, "execution", {key: value for key, value in thread.config["execution"].items() if key != "history"},
    )
    await journal(api).bind(session_id=thread.id, device_id=UUID(device_id), folder=FOLDER, nonce=NONCE)
    return project, master, await store.get_session(thread.id)


async def begin(api, thread_id: str) -> None:
    """Begin a thread made on the user's computer, as the server does once that computer has bound it."""
    state = api.app.state
    await thread_bound(state.session_factory, state.redis, UUID(thread_id), Binding("bound"))


async def has_begun(api, thread_id: str) -> bool:
    return bool(await events_of(api, UUID(thread_id), EventType.USER_MESSAGE))


async def held(api, master, name: str, **arguments) -> dict:
    """``call_tool`` by the worker holding *master*'s lease: a session on a computer answers only for that worker."""
    store = api.app.state.session_store
    lease = await store.try_acquire_lease(master.id, "worker-projects", ttl_seconds=60)
    try:
        return await call_tool(api, master, name, lease=lease, **arguments)
    finally:
        await store.release_lease(master.id, lease.lease_token)


async def made_local(api, laptop) -> tuple[dict, object, str]:
    """A project made on *laptop*'s computer, its master bound there, and a thread its master started there while the
    computer was away, which waits for it: *laptop* is not connected."""
    project = await create(api, execution=confirmed(laptop.device_id))
    master = await master_of(api, project)
    await laptop.app.connect()
    await eventually(lambda: is_bound(api, str(master.id)))
    await laptop.app.disconnect()
    started = await held(api, master, "agent", description="Check the totals", prompt="Check the totals in Budget.xlsx.")
    assert started["status"] == "waiting", started
    return project, master, started["agent_id"]


async def begun_in_a_cloud_project(api, laptop):
    """A project in the cloud, its master and a thread of it on *laptop*'s computer, begun there, as threads were made
    before every thread started in its project's own place; *laptop* is connected."""
    project = await create(api)
    master = await master_of(api, project)
    state = api.app.state
    thread = await make_thread(
        session_store=state.session_store, session_factory=state.session_factory, master=master, live_config=None,
        title="Check the totals", device_id=UUID(laptop.device_id), device_name="Flavius's ThinkPad", folder=FOLDER,
        start="Check the totals in Budget.xlsx.",
    )
    # The app binds a thread's copy in the name of a coordinator that works in the folder there.
    laptop.app.bindings[str(master.id)] = FOLDER
    await journal(api).bind(
        session_id=thread.id, device_id=UUID(laptop.device_id), folder=FOLDER, nonce=NONCE, history=thread.id, master=master.id,
    )
    await laptop.app.connect()
    await eventually(lambda: has_begun(api, str(thread.id)))
    return project, master, await state.session_store.get_session(thread.id)


async def begun_local(api, laptop):
    """A project, its master and a thread begun on *laptop*'s computer, which is connected."""
    project, master, thread_id = await made_local(api, laptop)
    await laptop.app.connect()
    await eventually(lambda: has_begun(api, thread_id))
    return project, master, await api.app.state.session_store.get_session(UUID(thread_id))


async def test_a_thread_on_the_users_computer_begins_once_its_computer_has_bound_it(api, laptop):
    project, master, thread_id = await made_local(api, laptop)
    assert await events_of(api, UUID(thread_id)) == [] and await rows(api, project) == []

    await laptop.app.connect()
    await eventually(lambda: has_begun(api, thread_id))
    # Bound to a copy of the folder of its own, which its app keeps.
    assert (laptop.app.bindings[thread_id], laptop.app.places.threads) == (FOLDER, {thread_id: thread_id})
    [row] = await rows(api, project)
    assert (row["id"], row["title"], row["group"]) == (thread_id, "Check the totals", "working")
    thread = await api.app.state.session_store.get_session(UUID(thread_id))
    [goal] = await events_of(api, thread.id, EventType.USER_MESSAGE)
    assert goal.data == {"content": "Check the totals in Budget.xlsx."}
    [spawned] = await events_of(api, master.id, EventType.WORKER_SPAWNED)
    assert spawned.data == {
        "worker_id": thread_id, "title": "Check the totals", "goal": "Check the totals in Budget.xlsx.",
        "bound": True, "format": NEW_FORMAT,
    }
    assert await queued(api, thread)


async def test_a_thread_its_computer_refuses_never_begins_and_its_master_hears_why(api, laptop):
    project, master, thread_id = await made_local(api, laptop)
    # Its coordinator's folder there was chosen again meanwhile: the app binds no thread in that coordinator's name.
    laptop.app.bindings.pop(str(master.id))
    await laptop.app.connect()
    await eventually(lambda: has_failed(api, thread_id))
    [failed] = await events_of(api, master.id, EventType.WORKER_FAILED)
    assert failed.data["worker_id"] == thread_id and "This folder was not confirmed on this computer" in failed.data["error"]
    assert (await api.app.state.session_store.get_session(UUID(thread_id))).status == "archived"
    assert await events_of(api, master.id, EventType.WORKER_SPAWNED) == [] and await rows(api, project) == []


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
    # Written in the thread's copy: the folder has it once the thread's turn lands.
    assert (laptop.app.places.copy(str(thread.id)) / "Totals.md").read_text() == "42\n"
    assert not (laptop.folder / "Totals.md").exists()


async def test_the_library_lists_a_local_threads_files_on_its_computer_and_the_clouds_as_they_are(api):
    # A project in the cloud with a thread on the user's computer, as one made before every thread started in its
    # project's own place.
    project = await create(api)
    master = await master_of(api, project)
    thread = await answered_by_the_journal(api, project, master)
    await upload(api, master, "Budget.xlsx", b"PK the cloud's")
    # A ref as the model gave it: from the folder's top, or the whole path there. One outside the folder is none
    # of its files, and nor is the folder itself.
    await turn_ends(api, thread, files=[
        "Budget.xlsx", f"{FOLDER}/Totals.md", "_artifacts/Report.pdf", "../Elsewhere/secret.txt", "/etc/hosts",
        ".", FOLDER,
    ])
    response = await library(api, project)
    assert response.status_code == 200, response.text
    here = {**PLACE, "device_id": thread.config["execution"]["device_id"], "online": False}
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
    """A thread of *master* on a new computer of the user's, begun as its binding is answered as its app answers."""
    device_id = UUID((await register(api))["id"])
    state = api.app.state
    thread = await make_thread(
        session_store=state.session_store, session_factory=state.session_factory, master=master, live_config=None,
        title="Check the totals", device_id=device_id, device_name="Flavius's ThinkPad", folder=FOLDER,
        start="Check the totals in Budget.xlsx.",
    )
    ops = journal(api)
    await ops.bind(session_id=thread.id, device_id=device_id, folder=FOLDER, nonce=NONCE, history=thread.id, master=master.id)
    [bind] = await ops.pending(device_id, 1)
    assert await ops.complete(device_id, 1, bind.id, bind.digest, {"ok": {"history": {"thread": str(thread.id)}}}) == "completed"
    return await state.session_store.get_session(thread.id)


async def test_a_thread_on_the_users_computer_works_in_its_copy_and_its_turn_lands_in_the_folder_and_reports_to_its_master(
    api, laptop, monkeypatch, tmp_path,
):
    _, master, thread = await begun_local(api, laptop)
    # A landing a killed worker left running in the project's files in the cloud, long quiet: a cloud thread's turn
    # end settles it, through the master's pod; a turn end on the user's computer lands there, and leaves it.
    left = f"left-{uuid4()}"
    async with api.app.state.session_factory() as db:
        db.add(WorkstreamHistory(
            workstream_id=UUID(thread.config["workstream_id"]), kind="landing", saga_id=left,
            saga_state="running", steps=[], agent_id=thread.agent_id, user_id=thread.user_id,
            updated_at=datetime.now(UTC) - timedelta(hours=1),
        ))
        await db.commit()
    # The harness reads the project's storage, as a worker's does.
    made = test_turn_sagas._make_loop_harness

    def with_storage(**kwargs):
        harness = made(**kwargs)
        harness._storage = api.app.state.storage
        return harness

    monkeypatch.setattr(test_turn_sagas, "_make_loop_harness", with_storage)
    pods = ThreadPods(tmp_path / "pods")
    pool = SandboxPool(pods)
    written: list[bool] = []

    async def during(harness) -> None:
        # While it works its file is in its copy alone: the folder is as the user left it.
        written.append((laptop.app.places.copy(str(thread.id)) / "Budget.xlsx").is_file() and not (laptop.folder / "Budget.xlsx").exists())

    harness = await a_turn(api, monkeypatch, thread, [
        calling(("write_file", {"path": "Budget.xlsx", "content": "Total,42\n"})),
        calling(("memory", {"action": "add", "content": "Checked."})),
        _final_response("The totals are in Budget.xlsx."),
    ], pool=pool, during=during)
    assert written == [True]
    # Its turn's end landed it in the folder, through its computer: no pod, and no copy in the cloud.
    assert (laptop.folder / "Budget.xlsx").read_text() == "Total,42\n"
    assert (pods.pods, pods.copies) == ({}, {})
    async with api.app.state.session_factory() as db:
        landed = (await db.execute(select(WorkstreamHistory).where(WorkstreamHistory.thread_id == thread.id))).scalars().all()
        assert await db.scalar(select(WorkstreamHistory.saga_state).where(WorkstreamHistory.saga_id == left)) == "running"
    assert [(row.saga_state, str(row.device_id), row.folder) for row in landed] == [("completed", laptop.device_id, FOLDER)]
    [report] = await events_of(api, master.id, EventType.WORKER_COMPLETE)
    assert (report.data["worker_id"], report.data["title"], report.data["result"]) == (
        str(thread.id), "Check the totals", "The totals are in Budget.xlsx.",
    )
    assert [(f["ref"], f["landing"]) for f in report.data["files"] if f["kind"] == "file"] == [("Budget.xlsx", "landed")]
    assert await queued(api, master)
    # The project's files in the cloud are not counted for its wake: its folder's history says, at each turn's open,
    # whether it can keep them.  And a file of its tools goes to its copy on its computer, never through a pod.
    assert "history_off" not in (await harness._with_history_cap(thread)).config
    assert not writes_to_copy(pool, sandbox_session_key(thread), thread.config)


async def test_a_helper_of_a_thread_on_the_users_computer_works_in_the_threads_copy(api, laptop):
    _, _, thread = await begun_local(api, laptop)
    store = api.app.state.session_store
    helper = await create_child_session(store=store, parent=thread, channel="worker")
    written = await execute_single_tool(
        {"id": "call_1", "function": {"name": "write_file", "arguments": json.dumps({"path": "Notes.md", "content": "42\n"})}},
        session=helper, lease=await store.try_acquire_lease(helper.id, "worker-local", ttl_seconds=60), store=store,
        tools=builtin_tools(), tenant=MagicMock(asset_root="/tmp/test"),
        redis=api.app.state.redis, session_factory=api.app.state.session_factory,
    )
    copy = laptop.app.places.copy(str(thread.id))
    assert (copy / "Notes.md").is_file(), written["content"]
    assert (copy / "Notes.md").read_text() == "42\n" and not (laptop.folder / "Notes.md").exists()
    # Through the thread's binding: the helper names the thread as its root and its copy, and is never bound itself.
    assert (helper.config["execution"], sandbox_session_key(helper)) == (thread.config["execution"], str(thread.id))
    assert "history_thread" not in helper.config
    with pytest.raises(ValueError, match="Only a root session is bound to a folder"):
        await journal(api).bind(session_id=helper.id, device_id=UUID(laptop.device_id), folder=FOLDER, nonce=NONCE)


@pytest.mark.parametrize("tool", ["agent", "background agent"])
async def test_a_thread_on_the_users_computer_starts_a_helper_in_its_copy_and_hands_no_copy_on(api, laptop, monkeypatch, tmp_path, tool):
    _, _, thread = await begun_local(api, laptop)
    store = api.app.state.session_store
    await store.update_session_config_key(thread.id, "coordinator", True)  # offered the tools that start workers and tasks
    thread = await store.get_session(thread.id)
    # A helper that is waited for: its outcome, without its turn.
    monkeypatch.setattr(delegate_module, "_poll_child_completion", AsyncMock(return_value={"status": "failed", "reason": "not run here"}))
    asked: list = []
    for name in ("keep_copy", "take_up", "project_lock"):
        monkeypatch.setattr(landing_module, name, lambda *args, name=name, **more: asked.append(name))
    pods = ThreadPods(tmp_path / "pods")
    pool = SandboxPool(pods)
    await asyncio.wait_for(a_turn(api, monkeypatch, thread, [
        calling((
            "agent" if tool == "background agent" else tool,
            {"description": "Check", "prompt": "Check the totals.", "run_in_background": tool == "background agent"}
            if tool.endswith("agent") else {"goal": "Check the totals."},
        )), _final_response("Started."),
        _final_response("The helper has it."),  # asked once more for the file its goal names
    ], pool=pool), 60)
    [helper] = await children_of(api, thread)
    # It works in the thread's copy on its computer, through the thread's binding: no copy of its own to start from.
    assert (helper.config["execution"], sandbox_session_key(helper)) == (thread.config["execution"], str(thread.id))
    assert copy_of(helper.config) == thread.id
    assert "history_thread" not in helper.config
    # So nothing is handed on or taken up: no pod, no copy, and the step asks for neither.
    assert (pods.pods, pods.copies, asked) == ({}, {}, [])
    assert not landing_module.handed_on(thread)


async def test_a_routine_run_of_a_project_whose_thread_works_on_a_folder_runs_on_the_projects_own_files_and_asks_the_computer_nothing(
    api, laptop, monkeypatch, pods,
):
    _, master, thread = await begun_in_a_cloud_project(api, laptop)
    (laptop.folder / "notes.txt").write_text("the folder's notes\n")
    pool = SandboxPool(pods)
    await a_history(api, pool, master)
    run, _ = await a_routine_run(api, master, "Health check")
    heard = len(laptop.app.received)
    await a_turn(api, monkeypatch, run, [
        calling(("terminal", {"command": "printf ' checked' >> notes.txt"})), _final_response("Checked."),
    ], pool=pool)
    # It ran in the master's pod, on the project's own files, and its change is recorded there as the routine's.
    assert (pods.project / "notes.txt").read_text() == "v1 notes\n checked"
    [*_, picked] = await pickups_of(api, master)
    assert (changed(picked), picked.steps[0]["arguments"]["author"]["name"], picked.device_id) == (["notes.txt"], "Health check", None)
    # The thread's computer heard nothing of it: its folder is as the user left it, and the thread has no copy yet.
    assert len(laptop.app.received) == heard
    assert sorted(p.name for p in laptop.folder.iterdir()) == ["notes.txt"]
    assert (laptop.folder / "notes.txt").read_text() == "the folder's notes\n"
    assert not laptop.app.places.copy(str(thread.id)).exists()
    # The thread's own turn lands in its folder what it did, and nothing of the routine's.
    await a_turn(api, monkeypatch, thread, [
        calling(("write_file", {"path": "Budget.xlsx", "content": "Total,42\n"})), _final_response("Done."),
    ])
    assert sorted(p.name for p in laptop.folder.iterdir()) == ["Budget.xlsx", "notes.txt"]
    assert (laptop.folder / "notes.txt").read_text() == "the folder's notes\n"
