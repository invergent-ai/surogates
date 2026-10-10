"""A project's thread bound with a copy of its own on the user's computer: its bind, and where its work goes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from surogates.devices.binding import THREAD_ACTIONS, THREAD_KINDS, Binding, copy_of
from surogates.devices.history import ComputerRefused, thread_copy
from surogates.devices.operations import CANCELLED_OUTCOME, OperationConflict, OperationRequest
from surogates.devices.store import REVOKED_OUTCOME, DeviceStore
from surogates.devices.workspace import DeviceOperationError
from surogates.harness.tool_exec import execute_single_tool
from surogates.session.provisioning import create_child_session
from surogates.workstreams.threads import make_thread
from surogates.tools.workspace_io import LocalWorkspaceIO
from tests.fake_laptop import FakeLaptop

from .conftest import create_org
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
    has_pending,
    link_url,
    register,
)
from .test_local_threads import begin, confirmed, device_card, journal, laptop, made_local, own_root  # noqa: F401  (laptop is a fixture)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def made_with_copy(api, device_id: str) -> tuple[dict, object, object]:
    """A project, its master and a thread made on the computer *device_id*, marked as one that works in a copy of its own.

    As the route that makes a thread will mark one: its ``execution`` names
    the thread whose copy it works in, which is itself.  Not bound yet.
    """
    project, master, proposal_id = await device_card(api)
    store = api.app.state.session_store
    thread = await make_thread(
        session_store=store, session_factory=api.app.state.session_factory, master=master, live_config=None,
        title="Check the totals", device=SimpleNamespace(id=UUID(device_id), name="Flavius's ThinkPad"), folder=FOLDER,
        card={"proposal_id": proposal_id, "key": "2"},
    )
    await store.update_session_config_key(
        thread.id, "execution", {**thread.config["execution"], "history": {"thread": str(thread.id)}},
    )
    return project, master, await store.get_session(thread.id)


async def bind_with_copy(api, thread) -> None:
    """Ask *thread*'s computer to bind it to a copy of its own, as the route that makes a thread will."""
    await journal(api).bind(
        session_id=thread.id, device_id=UUID(thread.config["execution"]["device_id"]), folder=FOLDER, nonce=NONCE,
        history=thread.id,
    )


async def test_no_bind_the_routes_send_names_a_copy(api):
    device = await register(api)
    project, _, proposal_id = await device_card(api)
    made = await api.client.post(
        f"/v1/workstreams/{project['id']}/threads",
        json={"proposal_id": proposal_id, "key": "2", "execution": confirmed(device["id"])}, headers=api.auth(),
    )
    assert made.status_code == 201, made.text
    chat = await api.client.post("/v1/sessions", json={"execution": confirmed(device["id"])}, headers=api.auth())
    assert chat.status_code == 201, chat.text
    binds = await journal(api).pending(UUID(device["id"]), 1)
    # A thread and a chat, each bound to the folder itself: no route asks a computer for a copy.
    assert sorted(str(bind.root_session_id) for bind in binds) == sorted([made.json()["thread_id"], chat.json()["id"]])
    assert [(bind.kind, bind.args) for bind in binds] == [("bind", {"folder": FOLDER, "nonce": NONCE})] * 2
    for session_id in (made.json()["thread_id"], chat.json()["id"]):
        assert copy_of((await api.app.state.session_store.get_session(UUID(session_id))).config) is None


async def test_a_bind_that_names_the_threads_copy_carries_it_and_the_thread_says_whose_copy_it_works_in(api):
    device = await register(api)
    _, _, thread = await made_with_copy(api, device["id"])
    await bind_with_copy(api, thread)
    [bind] = await journal(api).pending(UUID(device["id"]), 1)
    assert (bind.kind, bind.root_session_id, bind.args) == (
        "bind", thread.id, {"folder": FOLDER, "nonce": NONCE, "history": {"thread": str(thread.id)}},
    )
    assert await binding(api, thread.id) == Binding("pending")
    # What a worker reads: the thread works in its own copy, and so does a session made under it.
    assert copy_of(thread.config) == thread.id
    helper = await create_child_session(store=api.app.state.session_store, parent=thread, channel="worker")
    assert copy_of(helper.config) == thread.id


async def test_a_root_is_bound_with_a_copy_only_as_the_server_marked_it(api):
    device = await register(api)
    _, _, marked = await made_with_copy(api, device["id"])
    _, _, _, plain_id = await plain_thread(api, device["id"])
    ops = journal(api)
    bound = {"device_id": UUID(device["id"]), "folder": FOLDER, "nonce": NONCE}
    for case, (session_id, history) in {
        "a thread marked for a copy, bound to the folder itself": (marked.id, None),
        "a thread marked for a copy, bound to another's": (marked.id, uuid4()),
        "a thread bound to its folder, asked for a copy": (UUID(plain_id), UUID(plain_id)),
    }.items():
        with pytest.raises(ValueError, match="A root is bound with a copy only where the server made it a thread that works in one"):
            await ops.bind(session_id=session_id, history=history, **bound)
            pytest.fail(case)
    # The route's own bind of the plain thread stands as it was, and nothing was recorded for the marked one.
    [bind] = await ops.pending(UUID(device["id"]), 1)
    assert (bind.root_session_id, bind.args) == (UUID(plain_id), {"folder": FOLDER, "nonce": NONCE})
    assert await binding(api, marked.id) == Binding("failed", "The computer was never asked to set it up")


async def answered_bind(api, thread, outcome: dict) -> None:
    """*thread*'s computer answers its bind with *outcome*."""
    ops = journal(api)
    device_id = UUID(thread.config["execution"]["device_id"])
    [bind] = await ops.pending(device_id, 1)
    assert await ops.complete(device_id, 1, bind.id, bind.digest, outcome) == "completed"


KEEPS_NO_COPY = (
    "This chat's folder could not be set up: This computer keeps no copy of the folder for a thread: "
    "update Surogate Desktop. Start a new chat."
)


@pytest.mark.parametrize("case, outcome", [
    # As an app older than copies answers: it bound the thread to the folder itself.
    ("the folder itself", {"ok": None}),
    ("another thread's copy", {"ok": {"history": {"thread": "0f6d1c5e-7a3b-4c2d-9e1f-0a1b2c3d4e5f"}}}),
    ("something else", {"ok": {"history": {"thread": None}, "folder": FOLDER}}),
])
async def test_a_thread_whose_computer_does_not_say_it_keeps_the_copy_works_nowhere_and_says_so(api, case, outcome):
    device = await register(api)
    project, _, thread = await made_with_copy(api, device["id"])
    await bind_with_copy(api, thread)
    await answered_bind(api, thread, outcome)
    assert await binding(api, thread.id) == Binding(
        "failed", "This computer keeps no copy of the folder for a thread: update Surogate Desktop",
    ), case
    # Its user is told, and it never begins.
    refused = await begin(api, project, str(thread.id))
    assert (refused.status_code, refused.json()["detail"]) == (409, KEEPS_NO_COPY)
    # Nothing of the thread's reaches its computer: no file operation, no command, and none of its own kinds.
    ops = journal(api)
    for kind, args in [("write", {"key": f"{FOLDER}/a.txt", "data": ""}), ("run", {"command": "ls"}), ("history", {"action": "open"})]:
        with pytest.raises(DeviceOperationError, match="This session's folder is not set up on this computer yet"):
            await ops.run(OperationRequest(
                device_id=UUID(device["id"]), root_session_id=thread.id, calling_session_id=thread.id,
                invocation_id="open:0" if kind == "history" else "1", ordinal=1, kind=kind, args=args,
            ))
    assert await ops.pending(UUID(device["id"]), 1) == []


async def test_a_thread_whose_computer_says_it_keeps_the_copy_is_bound(api):
    device = await register(api)
    _, _, thread = await made_with_copy(api, device["id"])
    await bind_with_copy(api, thread)
    await answered_bind(api, thread, {"ok": {"history": {"thread": str(thread.id)}}})
    assert await binding(api, thread.id) == Binding("bound")


async def test_a_chat_on_the_folder_itself_is_bound_whatever_its_computer_says_beside_yes(api):
    device = await register(api)
    _, _, _, thread_id = await plain_thread(api, device["id"])
    thread = await api.app.state.session_store.get_session(UUID(thread_id))
    # As on master: a binding is its computer's yes, and nothing it says beside it is read.
    await answered_bind(api, thread, {"ok": {"history": {"thread": thread_id}}})
    assert await binding(api, thread.id) == Binding("bound")


async def plain_thread(api, device_id: str) -> tuple[dict, object, str, str]:
    """A thread made by the route on the computer *device_id*: bound to its folder itself."""
    return await made_local(api, SimpleNamespace(device_id=device_id))


async def bound_with_copy(api, device: dict | None = None) -> tuple[dict, dict, object]:
    """A registered computer, and a project and its thread made there and bound with a copy of its own, its computer's yes given."""
    device = device or await register(api)
    project, _, thread = await made_with_copy(api, device["id"])
    await bind_with_copy(api, thread)
    await answered_bind(api, thread, {"ok": {"history": {"thread": str(thread.id)}}})
    return device, project, thread


def own(kind: str, action: str) -> str:
    """The invocation a thread's own turn asks *action* of *kind* under."""
    if kind == "checkpoint":
        return "checkpoint:0:0:call_1"
    return "open:0" if (kind, action) == ("history", "open") else "land:0"


EACH = [(kind, action) for kind in sorted(THREAD_KINDS) for action in sorted(THREAD_ACTIONS[kind])]


def asked(device: dict, thread, kind: str, action: str, *, invocation: str | None = None, calling=None, **more) -> OperationRequest:
    """*action* of *kind* for *thread*'s copy, as its turn asks it unless told otherwise: each its own step of its invocation."""
    step = EACH.index((kind, action)) + 1 if (kind, action) in EACH else len(EACH) + 1
    return OperationRequest(
        device_id=UUID(device["id"]), root_session_id=thread.id, calling_session_id=(calling or thread).id,
        invocation_id=invocation or own(kind, action), ordinal=step, kind=kind, args={"action": action}, **more,
    )
NOT_ITS_OWN = "Only a thread's own turn asks its computer for its snapshots, its history or its landing"


async def recorded(api, request: OperationRequest) -> UUID:
    """*request* recorded in the journal, open, as the worker records it before it waits."""
    operation_id, outcome = await journal(api)._record(request)
    assert outcome is None
    return operation_id


async def test_each_of_a_threads_kinds_is_asked_only_under_its_turns_own_invocations(api):
    assert THREAD_KINDS == {"checkpoint", "history", "land"} and set(THREAD_ACTIONS) == THREAD_KINDS
    device, _, thread = await bound_with_copy(api)
    for kind, action in EACH:
        await recorded(api, asked(device, thread, kind, action))
        for other in ("17", "request:7d1b", "bind:x", "retire:x", "land", "open", "open0", "checkpoint", "x:land:0", "", " land:0"):
            if not other:
                continue
            with pytest.raises(ValueError, match=NOT_ITS_OWN):
                await journal(api)._record(asked(device, thread, kind, action, invocation=other))
                pytest.fail(f"{kind} {action} under {other!r}")
        # Each kind's prefix is its own: a history step is no snapshot's, and a snapshot no landing's.
        theirs = {"checkpoint:0:0:call_1", "open:0", "land:0"} - {own(kind, action)}
        if kind == "history" and action == "open":
            theirs.discard("land:0")
        for other in sorted(theirs):
            with pytest.raises(ValueError, match=NOT_ITS_OWN):
                await journal(api)._record(asked(device, thread, kind, action, invocation=other))
                pytest.fail(f"{kind} {action} under {other!r}")
    # An action no kind has is no thread's either.
    for kind in sorted(THREAD_KINDS):
        for action in (None, "prune", "close", "drop", "OPEN", 7):
            with pytest.raises(ValueError, match=NOT_ITS_OWN):
                await journal(api)._record(asked(device, thread, kind, action, invocation=own(kind, "x")))
                pytest.fail(f"{kind} {action!r}")


async def test_a_threads_snapshot_is_asked_by_any_session_of_the_threads_and_its_history_and_landing_by_the_thread_alone(api):
    device, _, thread = await bound_with_copy(api)
    helper = await create_child_session(store=api.app.state.session_store, parent=thread, channel="worker")
    for kind, action in EACH:
        request = asked(device, thread, kind, action, calling=helper)
        if kind == "checkpoint":
            await recorded(api, request)
            continue
        with pytest.raises(ValueError, match=NOT_ITS_OWN):
            await journal(api)._record(request)
            pytest.fail(f"{kind} {action} from a helper")


async def test_none_of_a_threads_kinds_is_asked_for_a_session_that_is_not_a_thread_bound_with_a_copy(api, session_factory):
    device = await register(api)
    # A project's thread bound to its folder itself, as every thread on master is.
    _, _, _, plain_id = await plain_thread(api, device["id"])
    plain = await api.app.state.session_store.get_session(UUID(plain_id))
    await answered_bind(api, plain, {"ok": None})
    # A chat on the folder.
    chat_id = await own_root(api, UUID(device["id"]))
    await journal(api).bind(session_id=chat_id, device_id=UUID(device["id"]), folder=FOLDER, nonce=NONCE)
    chat = await api.app.state.session_store.get_session(chat_id)
    await answered_bind(api, chat, {"ok": None})
    for session in (plain, chat):
        assert await binding(api, session.id) == Binding("bound")
        for kind, action in EACH:
            with pytest.raises(ValueError, match=NOT_ITS_OWN):
                await journal(api)._record(asked(device, session, kind, action))
                pytest.fail(f"{kind} {action} for {session.id}")


async def test_none_of_a_threads_kinds_is_asked_of_another_computer_or_another_users_or_organizations(api, session_factory):
    device, _, thread = await bound_with_copy(api)
    mine = await register(api)
    _, their_token = await add_user(session_factory, api.org_id)
    theirs = await register(api, token=their_token)
    stranger_org = await create_org(session_factory)
    stranger, _ = await add_user(session_factory, stranger_org)
    elsewhere = await DeviceStore(session_factory).create(org_id=stranger_org, agent_id=AGENT_ID, user_id=stranger, name="Elsewhere")
    strangers = {"id": str(elsewhere.device.id)}
    for computer in (mine, theirs, strangers):
        for kind, action in EACH:
            with pytest.raises(DeviceOperationError, match="This session does not work on this computer"):
                await journal(api)._record(asked(computer, thread, kind, action))
                pytest.fail(f"{kind} {action} on {computer['id']}")
    # Nor before its own computer has bound it.
    _, _, waiting = await made_with_copy(api, device["id"])
    await bind_with_copy(api, waiting)
    for kind, action in EACH:
        with pytest.raises(DeviceOperationError, match="This session's folder is not set up on this computer yet"):
            await journal(api)._record(asked(device, waiting, kind, action))
    for computer in (device, mine, theirs, strangers):
        assert [op.kind for op in await journal(api).pending(UUID(computer["id"]), 1)] == (["bind"] if computer is device else [])


@pytest.mark.parametrize("kind", sorted(THREAD_KINDS))
async def test_a_threads_own_kind_is_journaled_as_every_operation_is(api, session_factory, kind):
    device, _, thread = await bound_with_copy(api)
    device_id = UUID(device["id"])
    action = sorted(THREAD_ACTIONS[kind])[0]
    ops = journal(api)
    store = api.app.state.session_store
    lease = await store.try_acquire_lease(thread.id, "worker-a", ttl_seconds=60)
    request = asked(device, thread, kind, action, lease_token=str(lease.lease_token))
    # Asked twice at once, it is recorded once, and both askings hear its one outcome.
    first, again = (asyncio.create_task(ops.run(request)) for _ in range(2))
    await eventually(lambda: has_pending(ops, device_id))
    [open_] = await ops.pending(device_id, 1)
    assert (open_.kind, open_.args, open_.invocation_id) == (kind, {"action": action}, own(kind, action))
    assert await ops.complete(device_id, 1, open_.id, open_.digest, {"ok": {"x": 1}}) == "completed"
    assert await asyncio.wait_for(asyncio.gather(first, again), 10) == [{"ok": {"x": 1}}] * 2
    # Asked again, it is answered from the journal, and its computer is asked nothing.
    assert await ops.run(request) == {"ok": {"x": 1}}
    assert await ops.pending(device_id, 1) == []
    # Asked again with other arguments, it is refused: the turn took another path.
    with pytest.raises(OperationConflict):
        await ops.run(OperationRequest(**{**fields_of(request), "args": {"action": action, "more": True}}))
    # A worker that lost the thread's lease records nothing.
    await store.release_lease(thread.id, lease.lease_token)
    other = await store.try_acquire_lease(thread.id, "worker-b", ttl_seconds=60)
    stale = OperationRequest(**{**fields_of(request), "ordinal": 101})
    with pytest.raises(DeviceOperationError, match="Another worker runs this session now"):
        await ops.run(stale)
    assert await ops.pending(device_id, 1) == []
    # One that is stopped is closed, and its computer told.
    waiting = asyncio.create_task(ops.run(OperationRequest(**{**fields_of(stale), "lease_token": str(other.lease_token)})))
    await eventually(lambda: has_pending(ops, device_id))
    assert await ops.cancel([thread.id]) == 1
    assert await asyncio.wait_for(waiting, 10) == CANCELLED_OUTCOME
    # A stopped thread asks nothing new.
    await store.update_session_status(thread.id, "paused")
    with pytest.raises(DeviceOperationError, match="This session was stopped"):
        await ops.run(OperationRequest(**{**fields_of(stale), "ordinal": 102, "lease_token": str(other.lease_token)}))
    await store.update_session_status(thread.id, "active")
    # And a computer revoked answers nothing: its operation is closed with the revocation.
    assert (await api.client.delete(f"/v1/devices/{device_id}", headers=api.auth())).status_code == 204
    revoked = await ops.run(OperationRequest(**{**fields_of(stale), "ordinal": 103, "lease_token": str(other.lease_token)}))
    assert revoked == REVOKED_OUTCOME


def fields_of(request: OperationRequest) -> dict:
    return {name: getattr(request, name) for name in OperationRequest.__dataclass_fields__}


# -- on the tests' computer: an app that keeps a copy of the folder for each thread ------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def computer(api, link_url, tmp_path):
    """A registered computer's app that keeps threads' copies in its data, where the user confirmed ``FOLDER``; not connected yet.

    The folder holds two files of the user's, saved an hour ago, one only they may read.
    """
    device = await register(api)
    folder = (tmp_path / "laptop").resolve()
    (folder / "Plans").mkdir(parents=True)
    (folder / "Report.docx").write_bytes(b"PK report v1")
    (folder / "Plans" / "Q3.md").write_text("Q3 plan\n")
    os.chmod(folder / "Report.docx", 0o600)
    for path in (folder / "Report.docx", folder / "Plans" / "Q3.md", folder / "Plans"):
        os.utime(path, (time.time() - 3600, time.time() - 3600))
    app = FakeLaptop(link_url, device["token"], LocalWorkspaceIO(str(folder)), data=tmp_path / "laptop-data")
    app.prepare(NONCE, FOLDER)
    yield SimpleNamespace(app=app, device=device, device_id=device["id"], folder=folder)
    await app.disconnect()


async def connected_thread(api, computer):
    """A project's thread on *computer*, bound with a copy of its own by its app, which is connected."""
    project, _, thread = await made_with_copy(api, computer.device_id)
    await bind_with_copy(api, thread)
    await computer.app.connect()
    await eventually(lambda: is_bound(api, str(thread.id)))
    return project, thread


def picture(folder: Path) -> dict[str, tuple]:
    """Every entry of *folder*: its kind, mode, inode, size, times and, for a file, its bytes' hash. No link is followed."""
    seen = {}
    for path in sorted(folder.rglob("*")):
        info = path.lstat()
        data = hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else None
        seen[str(path.relative_to(folder))] = (
            stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, data,
        )
    return seen


async def tool(api, session, name: str, call: str = "call_1", **arguments) -> dict:
    """One tool call of *session*, as its turn makes it; the tool's answer."""
    store = api.app.state.session_store
    lease = await store.try_acquire_lease(session.id, "worker-local", ttl_seconds=60)
    try:
        result = await execute_single_tool(
            {"id": call, "function": {"name": name, "arguments": json.dumps(arguments)}},
            session=session, lease=lease, store=store, tools=builtin_tools(), tenant=MagicMock(asset_root="/tmp/test"),
            redis=api.app.state.redis, session_factory=api.app.state.session_factory,
        )
    finally:
        await store.release_lease(session.id, lease.lease_token)
    return json.loads(result["content"])


def blob(data: bytes) -> str:
    """*data*'s blob id, as git names a file's bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


async def test_a_thread_bound_with_a_copy_has_its_file_tools_work_in_the_copy_and_the_folder_does_not_change(api, computer):
    _, thread = await connected_thread(api, computer)
    copy = computer.app.places.copy(str(thread.id))
    before = picture(computer.folder)
    steps = [
        ("write_file", {"path": "Budget.xlsx", "content": "Total,42\n"}),
        ("read_file", {"path": "Plans/Q3.md"}),
        ("patch", {"mode": "replace", "path": "Plans/Q3.md", "old_string": "Q3 plan", "new_string": "Q3 plan, by the thread"}),
        ("terminal", {"command": f"echo noted > {computer.folder}/notes.txt && cat Plans/Q3.md"}),
        ("patch", {"mode": "patch", "patch": "*** Begin Patch\n*** Delete File: Report.docx\n\n*** End Patch"}),
        ("search_files", {"pattern": "by the thread"}),
    ]
    for number, (name, arguments) in enumerate(steps, 1):
        answer = await tool(api, thread, name, call=f"call_{number}", **arguments)
        assert not answer.get("error"), (name, answer)
        # The user's folder, entry by entry, is as it was after every step.
        assert picture(computer.folder) == before, name
    assert sorted(str(path.relative_to(copy)) for path in copy.rglob("*")) == ["Budget.xlsx", "Plans", "Plans/Q3.md", "notes.txt"]
    assert (copy / "Plans" / "Q3.md").read_text() == "Q3 plan, by the thread\n"
    assert (copy / "notes.txt").read_text() == "noted\n"
    # Its files are named by the folder's path, as the app names a copy's: the copy's own place is the app's.
    async with api.app.state.session_factory() as db:
        keys = (await db.execute(text(
            "SELECT outcome->>'ok' FROM device_operations WHERE calling_session_id = :id AND kind = 'resolve'"
        ), {"id": thread.id})).scalars().all()
    assert keys and all(key == str(computer.folder) or key.startswith(f"{computer.folder}/") for key in keys), keys


async def test_a_threads_own_kinds_reach_its_folders_history_and_the_land_kind_and_its_answers_are_taken_as_data(api, computer):
    _, thread = await connected_thread(api, computer)
    copy = thread_copy(thread, session_factory=api.app.state.session_factory, redis=api.app.state.redis, lease_token=None)
    assert await copy.open(0) == {"copy": "made"}
    assert (await tool(api, thread, "write_file", path="Plans/Q4.md", content="Q4 plan\n"))["status"] == "ok"
    taken = await copy.take(0, 1, "call_2", "before write_file")
    await tool(api, thread, "write_file", call="call_2", path="Budget.xlsx", content="Total,42\n")
    await copy.restore(0, taken)
    assert not (computer.app.places.copy(str(thread.id)) / "Budget.xlsx").exists()
    # A landing, as the worker will ask it: each step's answer checked as data on its way.
    saga = f"saga:{uuid4()}"
    trailers = [["Surogate-Saga", saga], ["Surogate-Thread", str(thread.id)]]
    landing = copy.steps("land:0")
    assert await landing.land("recover") == {"restored": [], "beside": [], "lost": [], "unread": []}
    assert await landing.history("changed") == {"paths": ["Plans/Q4.md"]}
    [[path, token]] = (await landing.land("revisions", paths=["Plans/Q4.md"]))["revisions"]
    assert (path, token) == ("Plans/Q4.md", "absent")
    picked = await landing.history("pickup", author={"name": "you", "email": "user:you@surogate"}, trailers=trailers)
    turn = await landing.history("commit", author={"name": "Check the totals", "email": f"thread:{thread.id}@surogate"}, trailers=trailers, pickup=picked["commit"])
    [change] = turn["changes"]
    assert await landing.land("apply", saga=saga, step=0, expected=token, **change) == {**change, "made": []}
    assert (computer.folder / "Plans" / "Q4.md").read_text() == "Q4 plan\n"
    recorded = await landing.history(
        "record", turn=turn["commit"], applied=[change], author={"name": "Check the totals", "email": f"thread:{thread.id}@surogate"},
        trailers=trailers, main=picked["main"], pickup=picked["commit"],
    )
    assert await landing.land("forget", saga=saga, applied=[change]) == {}
    assert computer.app.places.holder is None
    # The history holds the landing, and nothing it kept is left.
    assert (await landing.history("fetch", saga=saga, since=picked["main"]))["landing"] == recorded["commit"]
    assert list(computer.app.places.kept.glob("*")) == []


async def test_a_landing_its_folders_history_will_not_let_go_of_keeps_what_it_replaced_and_the_folder(api, computer):
    _, thread = await connected_thread(api, computer)
    copy = thread_copy(thread, session_factory=api.app.state.session_factory, redis=api.app.state.redis, lease_token=None)
    assert await copy.open(0) == {"copy": "made"}
    await tool(api, thread, "read_file", path="Plans/Q3.md")
    await tool(api, thread, "write_file", call="call_2", path="Plans/Q3.md", content="Q3 plan v2\n")
    saga = f"saga:{uuid4()}"
    landing = copy.steps("land:0")
    [[_, token]] = (await landing.land("revisions", paths=["Plans/Q3.md"]))["revisions"]
    change = {"path": "Plans/Q3.md", "before": blob(b"Q3 plan\n"), "after": blob(b"Q3 plan v2\n")}
    await landing.land("apply", saga=saga, step=0, expected=token, **change)
    assert (computer.folder / "Plans" / "Q3.md").read_text() == "Q3 plan v2\n"
    # Neither recorded nor put back: the history refuses, and the app forgets nothing and keeps the folder.
    with pytest.raises(ComputerRefused) as refused:
        await landing.land("forget", saga=saga, applied=[change])
    assert (refused.value.kind, refused.value.code) == ("history", "landing_unsettled")
    assert (computer.app.places.kept / saga / "0").read_bytes() == b"Q3 plan\n"
    assert computer.app.places.holder == str(thread.id)
    # Put back whole, it may go.
    assert await landing.land("unapply", saga=saga, step=0, path="Plans/Q3.md") == {"path": "Plans/Q3.md", "put_back": True}
    assert (computer.folder / "Plans" / "Q3.md").read_text() == "Q3 plan\n"
    assert await landing.land("forget", saga=saga, applied=[change]) == {}
    assert computer.app.places.holder is None


async def test_a_folder_with_no_history_has_its_thread_work_nowhere(api, computer):
    _, thread = await connected_thread(api, computer)
    computer.app.places.off = "cap"
    before = picture(computer.folder)
    copy = thread_copy(thread, session_factory=api.app.state.session_factory, redis=api.app.state.redis, lease_token=None)
    assert await copy.open(0) == {"history": "off", "reason": "cap"}
    written = await tool(api, thread, "write_file", path="Budget.xlsx", content="Total,42\n")
    assert "This folder has no history on this computer" in written["error"], written
    with pytest.raises(ComputerRefused) as refused:
        await copy.take(0, 1, "call_1", "before a step")
    assert refused.value.kind == "history_off"
    assert picture(computer.folder) == before
    assert not computer.app.places.copy(str(thread.id)).exists()


async def test_an_app_that_keeps_no_copies_binds_a_thread_that_works_nowhere(api, laptop):
    # As an app older than copies binds: the folder itself, and its yes says nothing of a copy.
    assert laptop.app.places is None
    project, _, thread = await made_with_copy(api, laptop.device_id)
    await bind_with_copy(api, thread)
    await laptop.app.connect()
    await eventually(lambda: has_failed(api, str(thread.id)))
    assert laptop.app.bindings == {str(thread.id): FOLDER}
    refused = await begin(api, project, str(thread.id))
    assert (refused.status_code, refused.json()["detail"]) == (409, KEEPS_NO_COPY)
    assert sorted(path.name for path in laptop.folder.iterdir()) == []
