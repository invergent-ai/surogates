"""Every step of a project's thread bound with a copy of its own starts from a snapshot of the copy, and a Stop puts the copy back.

Each scene runs a worker's real wake on the tests' computer, whose folder's
history is the real one: the turn's open, its steps, a Stop by the pause route
as a person stops a thread, and the undo that follows.  Only the model is a
script.  Around each, the user's folder is read entry by entry, and the copy
by its names, modes, bytes and, for a file the turn did not rewrite, its time.
"""

from __future__ import annotations

import asyncio
import re
import stat
from pathlib import Path
from uuid import UUID

import pytest

import surogates.devices.operations as operations_module
from surogates.devices.history import thread_copy
from surogates.devices.workspace import DeviceOperationError
from surogates.session.events import EventType
from surogates.session.provisioning import create_child_session
from tests.test_steer_loop import _final_response

from .test_device_sessions import is_bound
from .test_devices import FOLDER, api, eventually, link_url  # noqa: F401  (api and link_url are fixtures)
from .test_local_history_open import a_worker, asked, begun_with_copy, status_of, woken
from .test_local_history_threads import asked as request_of
from .test_local_history_threads import bound_with_copy, computer, picture, recorded  # noqa: F401  (computer is a fixture)
from .test_local_threads import begun_local, confirmed, journal, laptop  # noqa: F401  (laptop is a fixture)
from .test_turn_sagas import calling, saga_events

pytestmark = pytest.mark.asyncio(loop_scope="session")

ID = re.compile(r"[0-9a-f]{40}")
WRITE = calling(("write_file", {"path": "Budget.xlsx", "content": "Total,42\n"}))
EDIT = calling(("terminal", {"command": "printf ' edited' >> Report.docx"}))


def as_it_stands(copy: Path, *, rewritten: tuple[str, ...] = ()) -> dict[str, tuple]:
    """Every entry of *copy* but the harness's own folder: its kind, mode and bytes, and a file's time unless the turn rewrote it."""
    return {
        path: (kind, mode, data, mtime if kind == stat.S_IFREG and path not in rewritten else None)
        for path, (kind, mode, _, _, mtime, _, data) in picture(copy).items()
        if path.split("/")[0] != ".surogates-results"
    }


def stopped_by_its_user(api, thread):
    """A ``memory`` step's stand-in: the thread's user presses Stop, by the pause route, and the turn hears of it."""

    async def stop(harness) -> None:
        assert (await api.client.post(f"/v1/sessions/{thread.id}/pause", headers=api.auth())).status_code == 200
        # The interrupt the route publishes, as the worker hears it.
        harness.interrupt("paused by user")

    return stop


def pictured_at_its_start(worker, copy: Path, *, rewritten: tuple[str, ...] = ()) -> dict:
    """The copy as *worker*'s turn opened it, read once the open answered: what a Stop puts it back to."""
    started: dict = {}
    opened = worker._opened_for_the_turn

    async def pictured(session, lease) -> bool:
        went_on = await opened(session, lease)
        started.update(as_it_stands(copy, rewritten=rewritten))
        return went_on

    worker._opened_for_the_turn = pictured
    return started


async def calls_of(api, session) -> list[tuple[str, str | None]]:
    """Each tool call of *session*, as (its tool, the snapshot it started from)."""
    events = await api.app.state.session_store.get_events(session.id, types=[EventType.TOOL_CALL])
    return [(e.data["name"], e.data.get("checkpoint_hash")) for e in events]


async def steps_of(api, session) -> dict[str, tuple[str, str | None]]:
    """Each step of *session*'s sagas by its id: its tool, and the snapshot it began from."""
    return {
        data["step_id"]: (data["tool_name"], data.get("checkpoint_hash"))
        for kind, data in await saga_events(api, session.id) if kind == EventType.SAGA_STEP_BEGIN.value
    }


def holding_snapshots(computer):
    """*computer* takes its threads' snapshots and answers none until let go, as one away does; what it holds, and how to let go."""
    held: list = []
    handle = computer.app._handle

    async def holding(frame, ws) -> None:
        if frame["kind"] == "checkpoint" and frame["args"].get("action") == "take":
            held.append((frame, ws))
            return
        await handle(frame, ws)

    async def let_go() -> None:
        computer.app._handle = handle
        for frame, ws in held:
            await handle(frame, ws)

    computer.app._handle = holding
    return held, let_go


def copy_of_thread(api, thread):
    return thread_copy(thread, session_factory=api.app.state.session_factory, redis=api.app.state.redis, lease_token=None)


# -- a snapshot before every step ---------------------------------------------------------------------------------


async def test_every_step_of_a_local_thread_starts_from_a_snapshot_of_its_copy_and_the_folder_never_changes(api, computer, monkeypatch):
    _, _, thread = await begun_with_copy(api, computer)
    copy, folder = computer.app.places.copy(str(thread.id)), picture(computer.folder)
    worker = a_worker(api, monkeypatch, thread, [
        calling(("read_file", {"path": "Report.docx"})), WRITE, EDIT, _final_response("The totals are in Budget.xlsx."),
    ])
    started = pictured_at_its_start(worker, copy, rewritten=("Report.docx",))
    await worker.wake(thread.id)
    (read, none), (write, first), (terminal, second) = await calls_of(api, thread)
    assert (read, none, write, terminal) == ("read_file", None, "write_file", "terminal")
    assert ID.fullmatch(first) and ID.fullmatch(second) and first != second
    # Each under an invocation of its own, outside its step's tool call: the turn, the step's number in its saga, the call.
    assert asked(computer, "checkpoint") == [("checkpoint:0:0:call_0_write_file", "take"), ("checkpoint:0:1:call_0_terminal", "take")]
    # A step of the turn's saga begins from its snapshot, which a Stop puts the copy back to.
    assert list((await steps_of(api, thread)).values()) == [("write_file", first), ("terminal", second)]
    # Each is the copy as it stood before its step.
    back = copy_of_thread(api, thread)
    await back.restore(0, second)
    assert (copy / "Report.docx").read_bytes() == b"PK report v1" and (copy / "Budget.xlsx").read_text() == "Total,42\n"
    await back.restore(0, first)
    assert started and as_it_stands(copy, rewritten=("Report.docx",)) == started
    assert picture(computer.folder) == folder


async def test_a_step_run_again_after_its_worker_was_lost_is_answered_the_snapshot_it_took_and_its_computer_hears_of_it_once(
    api, computer, monkeypatch,
):
    _, _, thread = await begun_with_copy(api, computer)
    # The worker that began the turn opened the copy and took the step's snapshot, and was lost before its call.
    lost = copy_of_thread(api, thread)
    await lost.opened(0)
    taken = await lost.take(0, 0, "call_0_write_file", "before write_file")
    await woken(api, monkeypatch, thread, [WRITE, _final_response("Done.")])
    assert await calls_of(api, thread) == [("write_file", taken)]
    assert asked(computer, "checkpoint") == [("checkpoint:0:0:call_0_write_file", "take")]


async def test_a_snapshot_waits_for_its_computer_while_it_is_away_and_a_stop_meanwhile_ends_the_turn_with_nothing_more_run(api, computer, monkeypatch):
    monkeypatch.setattr(operations_module, "WAIT_GRACE_S", 0.1)
    _, _, thread = await begun_with_copy(api, computer)
    copy, folder = computer.app.places.copy(str(thread.id)), picture(computer.folder)
    held, let_go = holding_snapshots(computer)
    worker = a_worker(api, monkeypatch, thread, [WRITE])
    turn = asyncio.create_task(worker.wake(thread.id))

    async def holds() -> bool:
        return bool(held)

    await eventually(holds)
    await asyncio.sleep(0.3)
    # The step waits for its snapshot: nothing of it has run.
    assert not turn.done() and await calls_of(api, thread) == []
    await stopped_by_its_user(api, thread)(worker)
    await asyncio.wait_for(turn, 5)
    await let_go()
    await asyncio.sleep(0.3)
    # The step never ran, and its computer, answering at last, ran nothing of the stopped turn's.
    assert not (copy / "Budget.xlsx").exists() and picture(computer.folder) == folder
    assert asked(computer, "checkpoint") == [] and await status_of(api, thread) == "paused"



# -- only a thread's own turn ----------------------------------------------------------------------------------


async def test_only_a_threads_own_turn_takes_snapshots_and_one_that_is_no_commit_is_not_kept(api, computer, monkeypatch):
    _, _, thread = await begun_with_copy(api, computer)
    computer.app.lie = lambda frame, outcome: {"ok": {"hash": "refs/heads/main"}} if frame["kind"] == "checkpoint" else outcome
    await woken(api, monkeypatch, thread, [WRITE, _final_response("Done.")])
    # What the computer gave for a snapshot is no commit's id: the step ran with none, and nothing of it is recorded.
    assert await calls_of(api, thread) == [("write_file", None)] and list((await steps_of(api, thread)).values()) == [("write_file", None)]
    computer.app.lie = None
    taken = asked(computer, "checkpoint")
    # A helper works in its thread's copy, and takes no snapshot of its own, though its steps are a saga's.
    store = api.app.state.session_store
    helper = await create_child_session(store=store, parent=thread, channel="worker")
    await store.emit_event(helper.id, EventType.USER_MESSAGE, {"content": "Write the notes."})
    worker = a_worker(api, monkeypatch, helper, [calling(("write_file", {"path": "Notes.md", "content": "notes\n"})), _final_response("Noted.")])
    worker._saga_enabled = True
    await worker.wake(helper.id)
    assert [kind for kind, _ in await saga_events(api, helper.id)][:2] == [EventType.SAGA_START.value, EventType.SAGA_STEP_BEGIN.value]
    assert (computer.app.places.copy(str(thread.id)) / "Notes.md").read_text() == "notes\n"
    assert await calls_of(api, helper) == [("write_file", None)] and asked(computer, "checkpoint") == taken


async def test_a_thread_or_a_chat_on_its_folder_itself_takes_no_snapshot_and_its_steps_are_as_before(api, laptop, monkeypatch):
    _, _, thread = await begun_local(api, laptop)
    await woken(api, monkeypatch, thread, [WRITE, _final_response("Done.")])
    laptop.app.prepare("chat-binding-nonce-0001", FOLDER)
    created = await api.client.post(
        "/v1/sessions", json={"execution": confirmed(laptop.device_id, nonce="chat-binding-nonce-0001")}, headers=api.auth(),
    )
    assert created.status_code == 201, created.text
    await eventually(lambda: is_bound(api, created.json()["id"]))
    chat = await api.app.state.session_store.get_session(UUID(created.json()["id"]))
    await woken(api, monkeypatch, chat, [calling(("write_file", {"path": "Notes.md", "content": "notes\n"})), _final_response("Noted.")], said="Note it.")
    for session in (thread, chat):
        assert [hash_ for _, hash_ in await calls_of(api, session)] == [None]
        assert [hash_ for _, hash_ in (await steps_of(api, session)).values()] in ([None], [])
    assert "checkpoint" not in laptop.app.ran
    assert (laptop.folder / "Budget.xlsx").read_text() == "Total,42\n" and (laptop.folder / "Notes.md").read_text() == "notes\n"


# -- the journal --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["paused", "failed"])
async def test_a_stopped_threads_snapshot_is_refused_as_new_work_and_its_undo_still_reaches_its_computer(api, status):
    device, _, thread = await bound_with_copy(api)
    await api.app.state.session_store.update_session_status(thread.id, status)
    # A snapshot is a step's, and no step of a stopped turn starts: none is recorded, so none waits on after the Stop.
    with pytest.raises(DeviceOperationError, match="This session was stopped"):
        await journal(api)._record(request_of(device, thread, "checkpoint", "take", invocation="checkpoint:0:0:call_1"))
    assert await journal(api).pending(UUID(device["id"]), 1) == []
    # Its put-back is the Stop's own.
    await recorded(api, request_of(device, thread, "checkpoint", "restore", invocation=f"checkpoint:0:restore:{'a' * 40}"))
