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
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, text

import surogates.devices.operations as operations_module
from surogates.db.models import Event
from surogates.devices.history import thread_copy
from surogates.devices.workspace import DeviceOperationError
from surogates.governance.events import saga_start_event
from surogates.governance.saga import SagaOrchestrator
from surogates.governance.saga.compensator import STOP_LEFT_IN_COPY, STOP_LEFT_LANDED, WHY_NOT_TAKEN_BACK
from surogates.harness.loop_context_replay import worker_note
from surogates.harness.tool_exec import execute_single_tool
from surogates.session.events import EventType
from surogates.session.provisioning import create_child_session
from surogates.workstreams.threads import stop_thread
from tests.test_steer_loop import _final_response

from .test_device_sessions import is_bound
from .test_devices import FOLDER, api, builtin_tools, eventually, link_url  # noqa: F401  (api and link_url are fixtures)
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


async def undone(api, session) -> tuple[str, dict[str, str]]:
    """How the Stop's undo of *session*'s last turn ended: its saga's end, and why each step it did not take back was not, by its id."""
    sagas = await saga_events(api, session.id)
    *_, (kind, done) = sagas
    [*_, compensated] = [data for type_, data in sagas if type_ == EventType.SAGA_COMPENSATE.value]
    assert kind == EventType.SAGA_COMPLETE.value and done["saga_id"] == compensated["saga_id"]
    return done["status"], {entry["step_id"]: entry["why"] for entry in compensated.get("not_taken_back", [])}


async def what_the_stop_said(api, session) -> dict:
    """What its person reads where they stopped *session*: its last pause, which the chat draws as it draws a turn's failure."""
    *_, paused = await api.app.state.session_store.get_events(session.id, types=[EventType.SESSION_PAUSE])
    return paused.data


async def what_the_master_heard(api, thread) -> list[dict]:
    """Each report its master was told that *thread*'s Stop did not take all of its turn back."""
    reports = await api.app.state.session_store.get_events(thread.parent_id, types=[EventType.WORKER_COMPLETE])
    return [r.data for r in reports if r.data.get("worker_id") == str(thread.id) and r.data.get("stopped")]


def refusing_snapshots(frame, outcome):
    """A computer that refuses every snapshot, as a folder's history that fails."""
    if (frame["kind"], frame["args"]["action"]) == ("checkpoint", "take"):
        return {"error": {"type": "history", "code": "failed", "message": "The folder's history failed"}}
    return outcome


def running_unanswered(computer) -> list:
    """*computer* runs its threads' commands in their copies and answers none, as one still running them; those it ran."""
    ran: list = []
    handle = computer.app._handle

    async def running(frame, ws) -> None:
        if frame["kind"] != "run":
            await handle(frame, ws)
            return
        await computer.app._in_copy(frame)
        ran.append(frame)

    computer.app._handle = running
    return ran


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


async def landed(api, thread, turn: int) -> None:
    """What *thread*'s turn *turn* has done so far, landed in its folder by the computer's own steps, as a landing asks them."""
    landing = copy_of_thread(api, thread).steps(f"land:{turn}")
    saga = f"saga:{uuid4()}"
    trailers = [["Surogate-Saga", saga], ["Surogate-Thread", str(thread.id)]]
    author = {"name": "Check the totals", "email": f"thread:{thread.id}@surogate"}
    await landing.land("recover")
    paths = (await landing.history("changed"))["paths"]
    looked = dict((await landing.land("revisions", paths=paths))["revisions"])
    picked = await landing.history("pickup", author={"name": "you", "email": "user:you@surogate"}, trailers=trailers)
    committed = await landing.history("commit", author=author, trailers=trailers, pickup=picked["commit"])
    applied = [{**change, "step": step} for step, change in enumerate(committed["changes"])]
    for change in applied:
        await landing.land("apply", saga=saga, expected=looked[change["path"]], **change)
    await landing.history(
        "record", turn=committed["commit"], applied=committed["changes"], author=author, trailers=trailers,
        main=picked["main"], pickup=picked["commit"],
    )
    # Each apply named by the step it was applied under, as the computer forgets a landing.
    await landing.land("forget", saga=saga, applied=applied)


# -- a snapshot before every step ---------------------------------------------------------------------------------


async def test_every_step_of_a_local_thread_starts_from_a_snapshot_of_its_copy_and_neither_a_snapshot_nor_a_put_back_touches_its_folder(
    api, computer, monkeypatch,
):
    _, _, thread = await begun_with_copy(api, computer)
    copy, folder = computer.app.places.copy(str(thread.id)), picture(computer.folder)
    both = ("Report.docx", "Budget.xlsx")
    looked: dict = {}

    async def puts_each_back(harness) -> None:
        # Before the turn's end lands it: the copy put back to each snapshot in turn, the newest last, and read after each.
        *_, (_, first), (_, second), (_, third) = await calls_of(api, thread)
        looked["before"], back = as_it_stands(copy, rewritten=both), copy_of_thread(api, thread)
        await back.restore(0, second)
        looked["second"] = ((copy / "Report.docx").read_bytes(), (copy / "Budget.xlsx").read_text(), picture(computer.folder))
        await back.restore(0, first)
        looked["first"] = (as_it_stands(copy, rewritten=("Report.docx",)), picture(computer.folder))
        await back.restore(0, third)
        looked["third"] = (as_it_stands(copy, rewritten=both), picture(computer.folder))

    worker = a_worker(api, monkeypatch, thread, [
        calling(("read_file", {"path": "Report.docx"})), WRITE, EDIT, calling(("memory", {"action": "add", "content": "x"})),
        _final_response("The totals are in Budget.xlsx."),
    ], during=puts_each_back)
    started = pictured_at_its_start(worker, copy, rewritten=("Report.docx",))
    await worker.wake(thread.id)
    (read, none), (write, first), (terminal, second), (memory, third) = await calls_of(api, thread)
    assert (read, none, write, terminal, memory) == ("read_file", None, "write_file", "terminal", "memory")
    assert all(ID.fullmatch(hash_) for hash_ in (first, second, third)) and len({first, second, third}) == 3
    # Each under an invocation of its own, outside its step's tool call: the turn, the step's number in its saga, the call.
    assert asked(computer, "checkpoint") == [
        ("checkpoint:0:0:call_0_write_file", "take"), ("checkpoint:0:1:call_0_terminal", "take"), ("checkpoint:0:2:call_0_memory", "take"),
        *((f"checkpoint:0:restore:{hash_}", "restore") for hash_ in (second, first, third)),
    ]
    # A step of the turn's saga begins from its snapshot, which a Stop puts the copy back to.
    assert list((await steps_of(api, thread)).values()) == [("write_file", first), ("terminal", second), ("memory", third)]
    # Each is the copy as it stood before its step, and neither a snapshot nor a put-back touched the folder.
    assert looked["second"] == (b"PK report v1", "Total,42\n", folder)
    assert started and looked["first"] == (started, folder)
    assert looked["before"] and looked["third"] == (looked["before"], folder)
    # The copy put back where the turn left it, its end landed the turn's two files; every other entry is as it was.
    assert (computer.folder / "Budget.xlsx").read_text() == "Total,42\n"
    assert (computer.folder / "Report.docx").read_bytes() == b"PK report v1 edited"
    landed = picture(computer.folder)
    assert {path: entry for path, entry in landed.items() if path not in both} == {path: entry for path, entry in folder.items() if path not in both}


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


async def test_a_call_resumed_by_another_worker_takes_no_second_snapshot_and_a_stop_after_it_takes_it_back(api, computer, monkeypatch):
    _, _, thread = await begun_with_copy(api, computer)
    state = api.app.state
    store, copy = state.session_store, computer.app.places.copy(str(thread.id))
    # The worker that began the turn: its open, its saga, and its first step, run on the computer, its result never written.
    lease = await store.try_acquire_lease(thread.id, "worker-lost", ttl_seconds=60)
    await thread_copy(thread, session_factory=state.session_factory, redis=state.redis, lease_token=str(lease.lease_token)).opened(0)
    started = as_it_stands(copy, rewritten=("Report.docx",))
    message, _ = EDIT
    await store.emit_event(thread.id, EventType.LLM_RESPONSE, {"message": message})
    saga = SagaOrchestrator()
    begun = saga.create_saga(thread.id)
    await store.emit_event(thread.id, EventType.SAGA_START, saga_start_event(begun.saga_id, str(thread.id), begun.kind))
    await execute_single_tool(
        message["tool_calls"][0], session=thread, lease=lease, store=store, tools=builtin_tools(),
        tenant=MagicMock(asset_root="/tmp/test"), redis=state.redis, session_factory=state.session_factory, saga=saga,
    )
    async with state.session_factory() as db:
        await db.execute(delete(Event).where(
            Event.session_id == thread.id, Event.type.in_([EventType.TOOL_RESULT.value, EventType.SAGA_STEP_COMMITTED.value]),
        ))
        await db.commit()
    await store.release_lease(thread.id, lease.lease_token)
    assert (copy / "Report.docx").read_bytes() == b"PK report v1 edited"
    # The next worker resumes the call from the journal, and its user stops the turn after it.
    await woken(api, monkeypatch, thread, [calling(("memory", {"action": "add", "content": "x"})), _final_response("Done.")],
                during=stopped_by_its_user(api, thread))
    assert as_it_stands(copy, rewritten=("Report.docx",)) == started and (copy / "Report.docx").read_bytes() == b"PK report v1"
    takes = [under for under, action in asked(computer, "checkpoint") if action == "take"]
    assert takes == ["checkpoint:0:0:call_0_terminal", "checkpoint:0:1:call_0_memory"]
    assert await undone(api, thread) == ("completed", {})
    assert "error_title" not in await what_the_stop_said(api, thread) and await what_the_master_heard(api, thread) == []


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



# -- a Stop puts the copy back ------------------------------------------------------------------------------------


async def test_a_stop_by_its_user_puts_the_threads_copy_back_to_where_its_turn_started_and_the_folder_is_untouched(api, computer, monkeypatch):
    _, _, thread = await begun_with_copy(api, computer)
    copy, folder = computer.app.places.copy(str(thread.id)), picture(computer.folder)
    worker = a_worker(api, monkeypatch, thread, [
        calling(("write_file", {"path": "threads/Draft A/outline.md", "content": "outline"})),
        calling(("terminal", {"command": "printf ' edited' >> Report.docx && chmod 755 Plans/Q3.md"})),
        calling(("memory", {"action": "add", "content": "The memo is for the board."})),
        _final_response("Drafted."),
    ], during=stopped_by_its_user(api, thread))
    rewritten = ("Report.docx", "Plans/Q3.md")
    started = pictured_at_its_start(worker, copy, rewritten=rewritten)
    await worker.wake(thread.id)
    # The copy is where the turn started, the files it made gone and the one it changed as it was; the folder never changed.
    assert started and as_it_stands(copy, rewritten=rewritten) == started
    assert picture(computer.folder) == folder
    # The stopped thread's undo ran though it reads as paused: each step's snapshot put back, newest first, and nothing landed.
    taken = [hash_ for _, hash_ in await calls_of(api, thread)]
    assert asked(computer, "checkpoint")[3:] == [(f"checkpoint:0:restore:{hash_}", "restore") for hash_ in reversed(taken)]
    assert await undone(api, thread) == ("completed", {})
    assert await status_of(api, thread) == "paused" and asked(computer, "land") == []
    # A Stop that took everything back says nothing more than that it stopped.
    assert "error_title" not in await what_the_stop_said(api, thread) and await what_the_master_heard(api, thread) == []


async def test_each_stopped_turn_takes_its_own_snapshots_and_its_undo_puts_back_its_own_work(api, computer, monkeypatch):
    _, _, thread = await begun_with_copy(api, computer)
    store = api.app.state.session_store
    copy, folder = computer.app.places.copy(str(thread.id)), picture(computer.folder)
    turn = [
        calling(("terminal", {"command": "printf ' try' >> Report.docx && echo new > New.md"})),
        calling(("memory", {"action": "add", "content": "x"})), _final_response("Drafted."),
    ]

    async def stopped_turn() -> None:
        worker = a_worker(api, monkeypatch, await store.get_session(thread.id), turn, during=stopped_by_its_user(api, thread))
        started = pictured_at_its_start(worker, copy, rewritten=("Report.docx",))
        await worker.wake(thread.id)
        assert started and as_it_stands(copy, rewritten=("Report.docx",)) == started
        assert (copy / "Report.docx").read_bytes() == b"PK report v1" and not (copy / "New.md").exists()

    await stopped_turn()
    first = asked(computer)
    # You send the thread on, and stop it again.  Its model gives its calls the same ids.
    assert (await api.client.post(f"/v1/sessions/{thread.id}/resume", headers=api.auth())).status_code == 200
    await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "Go on."})
    await stopped_turn()
    second = asked(computer)[len(first):]
    name = second[0][0].removeprefix("open:")
    assert name != "0" and [action for _, action in second] == ["open", "take", "take", "restore", "restore"]
    assert [under for under, _ in second[:3]] == [f"open:{name}", f"checkpoint:{name}:0:call_0_terminal", f"checkpoint:{name}:1:call_0_memory"]
    # Each turn's undo is asked under that turn's own name: no answer of the stopped turn's before it is its own.
    assert all(under.startswith(f"checkpoint:{name}:restore:") for under, _ in second[3:])
    assert not {under for under, _ in second} & {under for under, _ in first}
    ends = [data["status"] for kind, data in await saga_events(api, thread.id) if kind == EventType.SAGA_COMPLETE.value]
    assert ends == ["completed", "completed"] and picture(computer.folder) == folder


async def test_a_step_whose_snapshot_its_computer_refused_runs_and_a_stop_takes_back_the_steps_after_it_saying_it_cannot_take_back_that_one(
    api, computer, monkeypatch,
):
    _, _, thread = await begun_with_copy(api, computer)
    copy, folder = computer.app.places.copy(str(thread.id)), picture(computer.folder)
    refusals = [1]

    def refuses_one_snapshot(frame, outcome):
        if (frame["kind"], frame["args"]["action"]) == ("checkpoint", "take") and refusals[0]:
            refusals[0] -= 1
            return {"error": {"type": "history", "code": "failed", "message": "The folder's history failed"}}
        return outcome

    computer.app.lie = refuses_one_snapshot
    worker = a_worker(api, monkeypatch, thread, [
        WRITE, EDIT, calling(("memory", {"action": "add", "content": "x"})), _final_response("Done."),
    ], during=stopped_by_its_user(api, thread))
    started = pictured_at_its_start(worker, copy, rewritten=("Report.docx",))
    await worker.wake(thread.id)
    (_, none), (_, second), (_, third) = await calls_of(api, thread)
    assert none is None and ID.fullmatch(second) and ID.fullmatch(third)
    # The Stop took back what it had a snapshot before; the step that had none it says, in words, it cannot take back.
    assert (copy / "Report.docx").read_bytes() == b"PK report v1" and (copy / "Budget.xlsx").read_text() == "Total,42\n"
    after = as_it_stands(copy, rewritten=("Report.docx",))
    assert started and {path: entry for path, entry in after.items() if path != "Budget.xlsx"} == started
    [write] = [step for step, (tool, _) in (await steps_of(api, thread)).items() if tool == "write_file"]
    assert await undone(api, thread) == ("escalated", {write: "no_snapshot"})
    assert picture(computer.folder) == folder
    # Said where its person stopped it, as the chat draws a turn's failure, and to its master as a thread's news.
    said = await what_the_stop_said(api, thread)
    assert (said["error_title"], said["error_category"], said["retryable"]) == (STOP_LEFT_IN_COPY, "storage_error", False)
    assert said["error_detail"] == f"write_file (Budget.xlsx): {WHY_NOT_TAKEN_BACK['no_snapshot']}"
    [heard] = await what_the_master_heard(api, thread)
    note = worker_note(EventType.WORKER_COMPLETE.value, heard)["content"]
    assert STOP_LEFT_IN_COPY in note and said["error_detail"] in note and "<<thread report>>" not in note


async def test_a_stop_whose_worker_no_longer_holds_the_thread_puts_nothing_back_and_says_so(api, computer, monkeypatch):
    _, _, thread = await begun_with_copy(api, computer)
    copy = computer.app.places.copy(str(thread.id))
    worker = a_worker(api, monkeypatch, thread, [EDIT, calling(("memory", {"action": "add", "content": "x"})), _final_response("Done.")],
                      during=stopped_by_its_user(api, thread))
    compensate = worker._compensate_sagas

    async def taken_over_first(*args, **kwargs) -> list:
        # Another worker has the thread now: its copy is that worker's turn's to work in.
        async with api.app.state.session_factory() as db:
            await db.execute(text(
                "UPDATE session_leases SET lease_token = :token, owner_id = 'worker-b' WHERE session_id = :id"
            ), {"token": uuid4(), "id": thread.id})
            await db.commit()
        return await compensate(*args, **kwargs)

    worker._compensate_sagas = taken_over_first
    await worker.wake(thread.id)
    assert (copy / "Report.docx").read_bytes() == b"PK report v1 edited"
    status, why = await undone(api, thread)
    assert status == "escalated" and list(why.values()) == ["not_asked", "not_asked"]
    assert (await what_the_stop_said(api, thread))["error_title"] == STOP_LEFT_IN_COPY
    assert [action for _, action in asked(computer, "checkpoint")] == ["take", "take"]


async def test_a_stop_after_a_landing_of_its_turn_takes_back_the_steps_since_and_says_the_landed_ones_cannot_be(api, computer, monkeypatch):
    _, _, thread = await begun_with_copy(api, computer)
    copy = computer.app.places.copy(str(thread.id))
    landing: dict = {}

    async def lands_then_stops(harness) -> None:
        if landing:
            await stopped_by_its_user(api, thread)(harness)
            return
        await landed(api, thread, 0)
        landing["folder"], landing["copy"] = picture(computer.folder), as_it_stands(copy)

    await woken(api, monkeypatch, thread, [
        WRITE, calling(("memory", {"action": "add", "content": "landed"})),
        calling(("write_file", {"path": "Notes.md", "content": "notes\n"})), calling(("memory", {"action": "add", "content": "stopped"})),
        _final_response("Done."),
    ], during=lands_then_stops)
    assert (computer.folder / "Budget.xlsx").read_text() == "Total,42\n"
    # The undo reached the copy alone: the folder is as the landing left it.
    assert picture(computer.folder) == landing["folder"]
    # What came after the landing is taken back; what landed is not, and the undo says why, in words.
    assert (copy / "Budget.xlsx").read_text() == "Total,42\n" and not (copy / "Notes.md").exists()
    assert as_it_stands(copy) == landing["copy"]
    steps = list(await steps_of(api, thread))
    assert await undone(api, thread) == ("escalated", {step: "landed" for step in steps[:2]})
    said = await what_the_stop_said(api, thread)
    assert said["error_title"] == STOP_LEFT_LANDED and said["error_detail"].count(WHY_NOT_TAKEN_BACK["landed"]) == 2
    assert [action for _, action in asked(computer, "checkpoint")] == ["take"] * 4 + ["restore"] * 4


@pytest.mark.parametrize("how", ["by its master", "by the pause route"])
async def test_a_step_with_no_snapshot_that_a_stop_cuts_short_is_named_as_not_taken_back_however_the_stop_came(
    api, computer, monkeypatch, how,
):
    _, _, thread = await begun_with_copy(api, computer)
    state = api.app.state
    copy, folder = computer.app.places.copy(str(thread.id)), picture(computer.folder)
    computer.app.lie = refusing_snapshots
    ran = running_unanswered(computer)
    worker = a_worker(api, monkeypatch, thread, [calling(("terminal", {"command": "echo partial > Partial.md"}))])
    turn = asyncio.create_task(worker.wake(thread.id))

    async def runs() -> bool:
        return bool(ran)

    await eventually(runs, timeout=10)
    if how == "by its master":
        await stop_thread(
            thread, reason="Stopped by its master.", interrupt="stopped by the master", session_store=state.session_store,
            session_factory=state.session_factory, redis=None,
        )
    else:
        assert (await api.client.post(f"/v1/sessions/{thread.id}/pause", headers=api.auth())).status_code == 200
    worker.interrupt("stopped")
    await asyncio.wait_for(turn, 10)
    # What the step did stays in the copy, and the undo says so: no snapshot was taken before it.
    assert (copy / "Partial.md").read_text() == "partial\n" and picture(computer.folder) == folder
    [step] = await steps_of(api, thread)
    assert await undone(api, thread) == ("escalated", {step: "no_snapshot"})
    said = await what_the_stop_said(api, thread)
    assert (said["error_title"], said["error_detail"]) == (STOP_LEFT_IN_COPY, f"terminal: {WHY_NOT_TAKEN_BACK['no_snapshot']}")
    [heard] = await what_the_master_heard(api, thread)
    assert [entry["why"] for entry in heard["not_taken_back"]] == ["no_snapshot"]


async def test_a_step_an_older_snapshot_put_back_or_one_that_never_ran_is_not_named_and_its_stop_says_nothing_more(
    api, computer, monkeypatch,
):
    _, _, thread = await begun_with_copy(api, computer)
    store = api.app.state.session_store
    await store.update_session_config_key(thread.id, "coordinator", True)  # offered the tools that start helpers
    thread = await store.get_session(thread.id)
    copy, folder = computer.app.places.copy(str(thread.id)), picture(computer.folder)
    refusals = [1]

    def refuses_the_second_snapshot(frame, outcome):
        # The first step takes none, the thread's own rule refusing it; of the others, the edit's is refused.
        if (frame["kind"], frame["args"]["action"]) == ("checkpoint", "take") and len(asked(computer, "checkpoint")) == 2 and refusals[0]:
            refusals[0] -= 1
            return refusing_snapshots(frame, outcome)
        return outcome

    computer.app.lie = refuses_the_second_snapshot
    worker = a_worker(api, monkeypatch, thread, [
        calling(("delegate_task", {"goal": "Research the market.", "agent_type": "deep-research"})),
        WRITE, EDIT, calling(("memory", {"action": "add", "content": "x"})), _final_response("Done."),
    ], during=stopped_by_its_user(api, thread))
    started = pictured_at_its_start(worker, copy, rewritten=("Report.docx",))
    await worker.wake(thread.id)
    assert [hash_ is None for _, hash_ in await calls_of(api, thread)] == [True, False, True, False]
    # The write's snapshot puts back the edit after it, which had none; the refused call never ran.
    assert started and as_it_stands(copy, rewritten=("Report.docx",)) == started and picture(computer.folder) == folder
    assert await undone(api, thread) == ("completed", {})
    assert "error_title" not in await what_the_stop_said(api, thread) and await what_the_master_heard(api, thread) == []


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
