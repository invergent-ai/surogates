"""A landing on the user's computer waits for its computer, and what a Stop, a revocation and a deletion do to it.

Each scene runs a worker's real wake on the tests' computer.  Its computer
is away as a link that drops once its app ran a step, before the step's
answer was sent: back, its app answers that step from what it kept, and runs
nothing twice.  A Stop is the pause route, then the interrupt the route
publishes reaching the turn's worker; a deletion is the delete route, then
its interrupt.  The scenes that write the folder run twice: on the tests'
computer's land rules, and on the app's own file helper in their place
(marked ``desktop``).  The folder is read back by name, mode, time and bytes.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid5

import pytest
from sqlalchemy import text, update

import surogates.devices.operations as operations_module
from surogates.db.models import WorkstreamHistory
from surogates.devices.history import Steps
from surogates.devices.operations import OperationRequest
from surogates.devices.workspace import DeviceOperationError
from surogates.governance.saga import Saga
from surogates.harness import local_landing
from surogates.session.events import EventType
from surogates.workstreams.history import start_landing
from surogates.workstreams.store import WorkstreamStore
from tests.test_fake_places import built_helper  # noqa: F401  (a fixture)
from tests.test_steer_loop import _final_response

from .test_devices import FOLDER, api, eventually, link_url  # noqa: F401  (api and link_url are fixtures)
from .test_local_history_landing import (  # noqa: F401  (here is a fixture)
    EDITS,
    LANDED,
    SMALL_FRAME,
    files_of,
    here,
    holding,
    kept,
    ran,
    records,
    said,
    seen,
    told,
)
from .test_local_history_open import a_worker, begun_with_copy, lease_let_go, status_of, woken
from .test_local_history_settle import Lost, lands_next, left_open, lost_after, two_threads
from .test_local_history_threads import asked, bound_with_copy, computer, fields_of, recorded  # noqa: F401  (computer is a fixture)
from .test_local_threads import journal
from .test_turn_sagas import calling
from .test_workstream_overview import rows
from .test_workstream_threads import events_of

pytestmark = pytest.mark.asyncio(loop_scope="session")


#: The snapshots ``EDITS``'s two steps start from, each asked before its tool call, as the computer runs them.
EDITS_TAKEN = [("checkpoint:0:0:call_0_write_file", "take"), ("checkpoint:0:1:call_1_terminal", "take")]


async def gone(here) -> bool:
    return not here.app.connected


def away_at(here, action: str, path: str | None = None, *, invocation: str = "land:0") -> None:
    """*here*'s app runs *action* under *invocation*, of *path* if named, then its link drops before the answer is sent."""
    def drop(frame, outcome):
        args = frame["args"]
        if frame["invocation_id"] == invocation and args.get("action") == action and path in (None, args.get("path")):
            here.app.reply = False
        return outcome

    here.app.lie = drop


async def back(here) -> None:
    """*here*'s computer comes back, and answers again: a step it ran is answered from what it kept."""
    here.app.lie, here.app.reply = None, True
    await here.app.connect()


async def landing_away(
    api, monkeypatch, here, thread, action: str, path: str | None = None, replies=None, *, invocation="land:0", during=None,
):
    """*thread*'s turn, whose landing finds its computer gone at *action*: the turn, waiting, and its worker."""
    monkeypatch.setattr(operations_module, "WAIT_GRACE_S", 0.1)
    away_at(here, action, path, invocation=invocation)
    worker = a_worker(api, monkeypatch, thread, replies or [EDITS, _final_response("Done.")], during=during)
    turn = asyncio.create_task(worker.wake(thread.id))
    await eventually(lambda: gone(here), timeout=20.0)
    return turn, worker


def heard_soon(monkeypatch) -> None:
    """A landing hears a Stop within a twentieth of a second here, and waits a fifth of one to give its folder back."""
    monkeypatch.setattr(local_landing, "STOP_CHECK", 0.05)
    monkeypatch.setattr(local_landing, "STOP_FORGET", 0.2)


async def stop(api, thread, worker) -> None:
    """You press Stop: the pause route, then the interrupt it publishes reaching the turn's worker."""
    assert (await api.client.post(f"/v1/sessions/{thread.id}/pause", headers=api.auth())).status_code == 200
    worker.interrupt("paused by user")


async def delete(api, thread, worker) -> None:
    """You delete the thread: the delete route, then the interrupt it publishes reaching the turn's worker."""
    assert (await api.client.delete(f"/v1/sessions/{thread.id}", headers=api.auth())).status_code == 204
    worker.interrupt("session deleted")


# -- a computer that is away ---------------------------------------------------------------------------------------


@pytest.mark.slow
async def test_a_landing_waits_for_a_computer_that_goes_away_and_holds_nothing_of_the_servers_meanwhile(api, here, monkeypatch):
    project, master, thread = await begun_with_copy(api, here)
    before = seen(here.folder)
    turn, _ = await landing_away(api, monkeypatch, here, thread, "commit")

    async def waiting() -> bool:
        [row] = await rows(api, project)
        return (row["group"], row["reason"], row["status_line"]) == ("working", "computer", "Waiting for Flavius's ThinkPad")

    await eventually(waiting, timeout=10.0)
    assert not turn.done() and seen(here.folder) == before
    # No lock of the server's is held while the computer is away, and no transaction is left open: its turn's lease alone.
    async with api.app.state.session_factory() as db:
        assert (await db.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"))).scalar() == 0
        assert (await db.execute(text(
            "SELECT count(*) FROM pg_stat_activity WHERE state = 'idle in transaction' AND pid <> pg_backend_pid()"
        ))).scalar() == 0
    await back(here)
    await asyncio.wait_for(turn, 30.0)
    assert (here.folder / "Budget.xlsx").read_text() == "Total,42\n" and (here.folder / "Report.docx").read_bytes() == b"PK report v1 edited"
    # The commit ran once: its answer came from what the computer kept.
    assert ran(here, "land:0") == LANDED and kept(here) == {} and here.app.places.holder is None
    [report] = await told(api, master)
    assert files_of(report) == [("Budget.xlsx", "landed", None), ("Report.docx", "landed", None)]


async def test_a_step_its_computer_does_not_answer_within_its_bound_ends_a_landing_that_wrote_no_file_with_nothing_left_of_it(
    api, here, monkeypatch,
):
    # The bound is a week; here it is half a second.
    monkeypatch.setattr(local_landing, "STEP_WAIT", 0.5)
    project, master, thread = await begun_with_copy(api, here)
    before = seen(here.folder)
    turn, _ = await landing_away(api, monkeypatch, here, thread, "commit")
    await asyncio.wait_for(turn, 30.0)
    # Its step failed at its bound: nothing of the landing is in the folder, nor left open to run when its computer is back.
    [row] = await records(api, thread)
    assert (row.saga_state, row.commit) == ("compensated", None)
    assert await left_open(api, thread) == [] and seen(here.folder) == before
    # Its master hears why; and nobody waits on you over a landing that wrote nothing.
    [report] = await told(api, master)
    assert (report.data["landing"], report.data["landing_reason"]) == ("compensated", "unanswered")
    assert "its computer did not answer" in said(report)
    [end] = await events_of(api, thread.id, EventType.SESSION_COMPLETE)
    assert end.data["saved"] is False
    [listed] = await rows(api, project)
    assert (listed["group"], listed["reason"]) == ("idle", None)
    # Not tried again at once, and its computer, back, is asked nothing more of it.
    await back(here)
    await asyncio.sleep(0.5)
    assert ran(here) == [("open:0", "open"), *EDITS_TAKEN, ("land:0:hold", "recover"), *(("land:0", action) for action in LANDED[:4])]
    assert seen(here.folder) == before


@pytest.mark.slow
async def test_a_landing_that_landed_whose_forgetting_its_computer_does_not_answer_ends_its_turn_and_is_forgotten_by_the_next(
    api, here, monkeypatch,
):
    # Thirty seconds; here it is one.
    monkeypatch.setattr(local_landing, "RELEASE_WAIT", 1.0)
    master, thread, _, next_ = await two_threads(api, monkeypatch, here)
    frames = holding(here, lambda frame: (frame["session_id"], frame["args"].get("action")) == (str(thread.id), "forget"))
    await asyncio.wait_for(woken(api, monkeypatch, thread, [EDITS, _final_response("Done.")]), 20.0)
    # Its files are in the folder, and its turn ended though its computer answered neither the forgetting nor the
    # giving back of its hold of the folder that followed.
    assert [frame["invocation_id"].split(":release:")[0] for frame in frames] == ["land:0", "land:0"]
    assert (here.folder / "Budget.xlsx").read_text() == "Total,42\n" and (here.folder / "Report.docx").read_bytes() == b"PK report v1 edited"
    [row] = await records(api, thread)
    assert row.saga_state == "completed" and [(s["tool_name"], s["state"]) for s in row.steps][-1] == ("history.forget", "failed")
    [report] = await told(api, master)
    assert files_of(report) == [("Budget.xlsx", "landed", None), ("Report.docx", "landed", None)]
    saga = f"saga:{uuid5(thread.id, 'land:0')}"
    assert set(kept(here)) == {f"{saga}/1"}
    # What it kept is let go by the next landing in its folder.
    del here.app._handle
    await lands_next(api, monkeypatch, here, next_)
    assert kept(here) == {} and (here.folder / "Y.md").read_text() == "y\n"


# -- a computer revoked --------------------------------------------------------------------------------------------


@pytest.mark.slow
async def test_a_computer_revoked_mid_landing_leaves_the_thread_waiting_on_you_over_a_half_landing(api, here, monkeypatch):
    project, master, thread = await begun_with_copy(api, here)
    edits = calling(("terminal", {"command": "printf 'more\\n' >> Plans/Q3.md && printf ' edited' >> Report.docx"}))
    turn, _ = await landing_away(api, monkeypatch, here, thread, "apply", "Plans/Q3.md", replies=[edits, _final_response("Done.")])
    assert (await api.client.delete(f"/v1/devices/{here.device_id}", headers=api.auth())).status_code == 204
    await asyncio.wait_for(turn, 30.0)
    # The computer wrote one file and can be asked nothing more: it stays, and what it replaced stays kept on that computer.
    assert (here.folder / "Plans" / "Q3.md").read_text() == "Q3 plan\nmore\n" and (here.folder / "Report.docx").read_bytes() == b"PK report v1"
    saga = f"saga:{uuid5(thread.id, 'land:0')}"
    assert {path: data for path, (_, data) in kept(here).items()} == {f"{saga}/0": b"Q3 plan\n"}
    [row] = await records(api, thread)
    assert (row.saga_state, row.commit) == ("escalated", None)
    # Its record says which file could not be put back, and which was never written.
    assert [(s["arguments"]["path"], s["state"]) for s in row.steps if s["tool_name"] == "history.apply"] == [
        ("Plans/Q3.md", "compensation_failed"), ("Report.docx", "pending"),
    ]
    [report] = await told(api, master)
    assert report.data["landing"] == "escalated"
    # The thread waits on you over them.
    [waits] = await events_of(api, thread.id, EventType.INBOX_ACTION_REQUIRED)
    assert (waits.data["title"], waits.data["reason"], waits.data["escalated"]) == ("Couldn't finish landing my changes", "files", True)
    [listed] = await rows(api, project)
    assert (listed["group"], listed["reason"], listed["status_line"]) == ("waiting", "files", "Couldn't finish landing my changes")


# -- a Stop --------------------------------------------------------------------------------------------------------


#: Where a landing that has written no file waits for its computer when you press Stop: the step, and the steps it ran.
BEFORE_ANY_FILE = {
    "its hold of the folder": ("land:0:hold", "recover", []),
    "its first step": ("land:0", "changed", LANDED[:1]),
    "its look": ("land:0", "revisions", LANDED[:2]),
    "its commit": ("land:0", "commit", LANDED[:4]),
}


@pytest.mark.parametrize("at", sorted(BEFORE_ANY_FILE))
async def test_a_stop_ends_a_landing_that_has_written_no_file_and_nothing_of_it_lands_when_its_computer_is_back(
    api, here, monkeypatch, at,
):
    heard_soon(monkeypatch)
    _, master, thread = await begun_with_copy(api, here)
    before = seen(here.folder)
    invocation, action, landed = BEFORE_ANY_FILE[at]
    turn, worker = await landing_away(api, monkeypatch, here, thread, action, invocation=invocation)
    await stop(api, thread, worker)
    # It ends where it stands, though its computer is away: the step it waited in is closed, and nothing else is left open.
    await asyncio.wait_for(turn, 10.0)
    assert await left_open(api, thread) == []
    assert [(row.saga_state, row.commit) for row in await records(api, thread)] == ([("compensated", None)] if landed else [])
    # Back, its computer is asked nothing more of it: a commit it had run moved the thread's branch, and no file of the folder.
    await back(here)
    await asyncio.sleep(0.5)
    assert ran(here) == [("open:0", "open"), *EDITS_TAKEN, ("land:0:hold", "recover"), *(("land:0", step) for step in landed)]
    assert seen(here.folder) == before
    # The thread stays as the Stop left it, its work in its copy; its master hears that none of it landed, and why.
    assert await status_of(api, thread) == "paused" and await lease_let_go(api, thread)
    assert (here.app.places.copy(str(thread.id)) / "Budget.xlsx").read_text() == "Total,42\n"
    [report] = await told(api, master)
    assert (report.data["landing"], report.data["landing_reason"]) == ("compensated", "stopped")
    assert {landed for _, landed, _ in files_of(report)} <= {"not_merged"} and "landing_stopped" not in report.data
    assert "the thread was stopped before its landing wrote a file" in said(report)
    [end] = await events_of(api, thread.id, EventType.SESSION_COMPLETE)
    assert end.data["saved"] is False and "stopped" not in end.data


async def test_a_stop_heard_once_its_turn_is_committed_ends_the_landing_before_its_first_apply(api, here, monkeypatch):
    # Heard only as its steps end: none is waited for long enough to hear it while it waits.
    monkeypatch.setattr(local_landing, "STOP_CHECK", 30.0)
    monkeypatch.setattr(local_landing, "STOP_FORGET", 0.2)
    _, master, thread = await begun_with_copy(api, here)
    before = seen(here.folder)
    worker = a_worker(api, monkeypatch, thread, [EDITS, _final_response("Done.")])
    ask = Steps.ask

    async def stopped_at_the_commit(self, kind, action, **arguments):
        answer = await ask(self, kind, action, **arguments)
        if action == "commit":
            await stop(api, thread, worker)
        return answer

    monkeypatch.setattr(Steps, "ask", stopped_at_the_commit)
    await asyncio.wait_for(worker.wake(thread.id), 30.0)
    # Its last moment: nothing was applied, and the folder was given back.
    assert ran(here, "land:0") == LANDED[:4] and ran(here, "land:0:release") == ["forget"]
    assert seen(here.folder) == before and here.app.places.holder is None
    [row] = await records(api, thread)
    assert row.saga_state == "compensated"
    [report] = await told(api, master)
    assert (report.data["landing"], report.data["landing_reason"]) == ("compensated", "stopped")
    assert await status_of(api, thread) == "paused"


@pytest.mark.parametrize("at", ["an apply", "the record"])
async def test_a_stop_while_a_step_is_in_flight_of_a_landing_that_has_written_a_file_does_not_interrupt_it(api, here, monkeypatch, at):
    heard_soon(monkeypatch)
    _, master, thread = await begun_with_copy(api, here)
    action, path = ("apply", "Report.docx") if at == "an apply" else ("record", None)
    turn, worker = await landing_away(api, monkeypatch, here, thread, action, path)
    await stop(api, thread, worker)
    # The step it waits in is the landing's own: a landing that has written a file finishes, or is put back whole, as with no Stop.
    await asyncio.sleep(0.5)
    [row] = await records(api, thread)
    assert not turn.done() and row.saga_state == "running" and ("land:0", action) in await left_open(api, thread)
    await back(here)
    await asyncio.wait_for(turn, 30.0)
    assert (here.folder / "Budget.xlsx").read_text() == "Total,42\n" and (here.folder / "Report.docx").read_bytes() == b"PK report v1 edited"
    assert ran(here, "land:0") == LANDED and kept(here) == {} and here.app.places.holder is None
    [row] = await records(api, thread)
    assert row.saga_state == "completed"
    # Whoever stopped it is told that its landing was already under way, and how it ended; the thread stays as the Stop left it.
    [report] = await told(api, master)
    assert files_of(report) == [("Budget.xlsx", "landed", None), ("Report.docx", "landed", None)] and report.data["landing_stopped"] is True
    assert "Stopped while its landing was already under way, so the landing was not interrupted: it finished" in said(report)
    [end] = await events_of(api, thread.id, EventType.SESSION_COMPLETE)
    assert (end.data["saved"], end.data["stopped"]) == (True, "landing")
    assert await status_of(api, thread) == "paused" and await lease_let_go(api, thread)


@pytest.mark.parametrize("at", ["a turn that changed nothing", "your edits too many for one landing"])
async def test_a_stop_while_a_landing_that_wrote_nothing_waits_in_its_forgetting_ends_its_turn_within_a_bound(api, here, monkeypatch, at):
    heard_soon(monkeypatch)
    # Thirty seconds; here it is half a second.
    monkeypatch.setattr(local_landing, "RELEASE_WAIT", 0.5)
    _, master, thread = await begun_with_copy(api, here)
    if at == "a turn that changed nothing":
        replies, during = [calling(("terminal", {"command": "ls"})), _final_response("Done.")], None
    else:
        here.app.frame_chars = SMALL_FRAME
        photos = here.folder / "Photos" / "2026"
        replies = [
            calling(("write_file", {"path": "Budget.xlsx", "content": "Total,42\n"})),
            calling(("memory", {"action": "add", "content": "Done."})), _final_response("Done."),
        ]

        async def during(harness) -> None:
            # While the thread works, you add more photos to the folder than one landing's answer can carry.
            photos.mkdir(parents=True)
            for n in range(80):
                (photos / f"IMG_{n:06d}.jpg").write_bytes(b"\xff\xd8 photo %d" % n)

    turn, worker = await landing_away(api, monkeypatch, here, thread, "forget", replies=replies, during=during)
    before = seen(here.folder)
    await stop(api, thread, worker)
    # Its turn ends though its computer is away, and lets its lease go: nothing was written, so nothing is lost.
    await asyncio.wait_for(turn, 10.0)
    assert ran(here, "land:0")[-1] == "forget" and await records(api, thread) == [] and await left_open(api, thread) == []
    assert await status_of(api, thread) == "paused" and await lease_let_go(api, thread)
    assert seen(here.folder) == before
    # Nothing of it was under way, and its master is not told it was.
    [report] = await told(api, master)
    assert "landing_stopped" not in report.data
    [end] = await events_of(api, thread.id, EventType.SESSION_COMPLETE)
    assert "stopped" not in end.data


async def test_a_stop_heard_once_its_landing_wrote_is_told_as_such_however_the_landing_ends_and_it_is_not_tried_again(api, here, monkeypatch):
    heard_soon(monkeypatch)
    _, master, thread = await begun_with_copy(api, here)
    report_file = here.folder / "Report.docx"
    worker = a_worker(api, monkeypatch, thread, [EDITS, _final_response("Done.")])
    ask = Steps.ask

    async def stopped_after_the_first_apply(self, kind, action, **arguments):
        answer = await ask(self, kind, action, **arguments)
        if action == "apply" and arguments["path"] == "Budget.xlsx":
            # You press Stop, and you save the report the landing is about to write.
            await stop(api, thread, worker)
            report_file.write_bytes(b"PK report v2, by you")
        return answer

    monkeypatch.setattr(Steps, "ask", stopped_after_the_first_apply)
    await asyncio.wait_for(worker.wake(thread.id), 30.0)
    # Its apply of the report was refused, and the landing put back whole; a Stop heard, it is not tried again.
    assert ran(here, "land:0") == [*LANDED[:6], "unapply", "unapply", "forget"] and ran(here, "land:0:2") == []
    assert report_file.read_bytes() == b"PK report v2, by you" and not (here.folder / "Budget.xlsx").exists()
    [report] = await told(api, master)
    assert (report.data["landing"], report.data["landing_reason"], report.data["landing_stopped"]) == ("compensated", "changed", True)
    assert "Stopped while its landing was already under way, so the landing was not interrupted: it was put back whole" in said(report)
    assert await status_of(api, thread) == "paused"


@pytest.mark.slow
async def test_a_stop_heard_while_a_resumed_turn_settles_its_first_landing_lands_nothing_more_of_what_it_did_since(api, here, monkeypatch):
    heard_soon(monkeypatch)
    _, master, thread = await begun_with_copy(api, here)
    with monkeypatch.context() as first_run:
        lost_after(first_run, "record", 1)
        with pytest.raises(Lost):
            await woken(api, monkeypatch, thread, [EDITS, _final_response("Done.")])
    # Taken up again, the turn works on: its first landing, which recorded, is settled, and you press Stop meanwhile.
    worker = a_worker(api, monkeypatch, thread, [calling(("write_file", {"path": "More.md", "content": "more\n"})), _final_response("Done.")])
    ask = Steps.ask

    async def stopped_in_the_settle(self, kind, action, **arguments):
        answer = await ask(self, kind, action, **arguments)
        if action == "fetch":
            await stop(api, thread, worker)
        return answer

    monkeypatch.setattr(Steps, "ask", stopped_in_the_settle)
    await asyncio.wait_for(worker.wake(thread.id), 30.0)
    # What it landed stands; what it did since stays in its copy, for its next turn.
    [first] = await records(api, thread)
    assert first.saga_state == "completed" and ran(here, "land:0:3") == []
    assert not (here.folder / "More.md").exists() and (here.app.places.copy(str(thread.id)) / "More.md").read_text() == "more\n"
    [report] = await told(api, master)
    assert "landing" not in report.data and report.data["landing_stopped"] is True
    assert await status_of(api, thread) == "paused"


@pytest.mark.slow
async def test_a_stop_while_a_landing_settles_another_left_in_its_folder_ends_the_settle_where_it_stands(api, here, monkeypatch):
    heard_soon(monkeypatch)
    lost_master, lost, next_master, next_ = await two_threads(api, monkeypatch, here)
    with monkeypatch.context() as first_run:
        lost_after(first_run, "apply", 1)
        with pytest.raises(Lost):
            await woken(api, monkeypatch, lost, [EDITS, _final_response("Done.")])
    here.app.places.holder = None
    # The next thread's landing settles the lost one first, and finds its computer gone at its first put-back.
    turn, worker = await landing_away(
        api, monkeypatch, here, next_, "unapply", invocation=f"land:0:settle:saga:{uuid5(lost.id, 'land:0')}",
        replies=[calling(("write_file", {"path": "Y.md", "content": "y\n"})), _final_response("Done.")],
    )
    await stop(api, next_, worker)
    await asyncio.wait_for(turn, 10.0)
    # It ended where it stood: the lost landing is left running, for the next landing in the folder, and nothing of the
    # stopped thread's own was looked at there.
    [row] = await records(api, lost)
    assert row.saga_state == "running" and await records(api, next_) == [] and await left_open(api, next_) == []
    [report] = await told(api, next_master)
    assert (report.data["landing"], report.data["landing_reason"]) == ("compensated", "stopped")
    assert await status_of(api, next_) == "paused"


async def test_a_stop_at_a_turn_end_with_nothing_to_land_leaves_the_thread_stopped(api, computer, monkeypatch):
    _, master, thread = await begun_with_copy(api, computer)
    before = seen(computer.folder)
    worker = a_worker(api, monkeypatch, thread, [_final_response("Nothing in the folder needs to change.")])
    lands = local_landing.lands

    async def stopped_as_it_ends(*args, **kwargs) -> bool:
        # You press Stop once the turn's end has found it has nothing to land.
        anything = await lands(*args, **kwargs)
        await stop(api, thread, worker)
        return anything

    monkeypatch.setattr(local_landing, "lands", stopped_as_it_ends)
    await asyncio.wait_for(worker.wake(thread.id), 30.0)
    # It had nothing to land, and its computer was asked nothing for it: the thread stays as the Stop left it.
    assert ran(computer) == [("open:0", "open")] and seen(computer.folder) == before
    assert await status_of(api, thread) == "paused" and await lease_let_go(api, thread)


# -- a deletion ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("at", ["its first apply", "its second apply", "its record"])
async def test_a_thread_deleted_while_its_landing_is_under_way_has_it_put_back_whole_by_its_worker_or_finished_once_recorded(
    api, here, monkeypatch, at,
):
    heard_soon(monkeypatch)
    _, master, thread = await begun_with_copy(api, here)
    before = seen(here.folder)
    action, path = {"its first apply": ("apply", "Budget.xlsx"), "its second apply": ("apply", "Report.docx"), "its record": ("record", None)}[at]
    turn, worker = await landing_away(api, monkeypatch, here, thread, action, path)
    await delete(api, thread, worker)
    # Back, its computer answers the step in flight; then the landing's worker ends it, though its thread is deleted.
    await back(here)
    await asyncio.wait_for(turn, 30.0)
    assert kept(here) == {} and here.app.places.holder is None and await left_open(api, thread) == []
    [row] = await records(api, thread)
    [report] = await told(api, master)
    if at == "its record":
        # It had recorded: its files stand, and it is forgotten.
        assert ran(here, "land:0") == LANDED and row.saga_state == "completed"
        assert files_of(report) == [("Budget.xlsx", "landed", None), ("Report.docx", "landed", None)]
        assert "landing" not in report.data and "landing_stopped" not in report.data
    else:
        # Put back whole, newest first, nothing more applied, and forgotten.
        written = ["apply"] if at == "its first apply" else ["apply", "apply"]
        assert ran(here, "land:0") == [*LANDED[:4], *written, *["unapply"] * len(written), "forget"]
        assert seen(here.folder) == before and row.saga_state == "compensated"
        assert (report.data["landing"], report.data["landing_reason"]) == ("compensated", "deleted")
        assert "the thread was deleted before its landing finished" in said(report) and "lands with its next turn" not in said(report)
    # Its computer was told to forget the folder, as for any deleted thread.
    assert "retire" in here.app.ran


@pytest.mark.parametrize("at", ["its hold of the folder", "its commit"])
async def test_a_thread_deleted_before_its_landings_first_apply_has_that_landing_end_there_and_says_it_was_deleted(api, here, monkeypatch, at):
    heard_soon(monkeypatch)
    _, master, thread = await begun_with_copy(api, here)
    before = seen(here.folder)
    held = at == "its hold of the folder"
    turn, worker = await landing_away(
        api, monkeypatch, here, thread, "recover" if held else "commit", invocation="land:0:hold" if held else "land:0",
    )
    await delete(api, thread, worker)
    await asyncio.wait_for(turn, 10.0)
    # It ends where it stood, with nothing of it in the folder and nothing of it left to run: its computer, away, is
    # still to be told to forget the folder, as for any deleted thread.
    assert [row.saga_state for row in await records(api, thread)] == ([] if held else ["compensated"])
    assert await left_open(api, thread) == [("retire", None)] and seen(here.folder) == before
    # Its master hears that it was deleted, and nothing of a next turn it will not have.
    [report] = await told(api, master)
    assert (report.data["landing"], report.data["landing_reason"]) == ("compensated", "deleted")
    assert "the thread was deleted before its landing finished" in said(report) and "lands with its next turn" not in said(report)


async def test_a_thread_deleted_between_two_applies_whose_interrupt_comes_after_the_next_apply_has_its_landing_put_back_whole(
    api, here, monkeypatch,
):
    heard_soon(monkeypatch)
    _, master, thread = await begun_with_copy(api, here)
    before = seen(here.folder)
    worker = a_worker(api, monkeypatch, thread, [EDITS, _final_response("Done.")])
    ask = Steps.ask

    async def deleted_after_the_first_apply(self, kind, action, **arguments):
        answer = await ask(self, kind, action, **arguments)
        if action == "apply" and arguments["path"] == "Budget.xlsx":
            # Its computer answers on; the route's interrupt reaches the worker only once its next step has been refused.
            assert (await api.client.delete(f"/v1/sessions/{thread.id}", headers=api.auth())).status_code == 204
            asyncio.get_running_loop().call_later(0.2, worker.interrupt, "session deleted")
        return answer

    monkeypatch.setattr(Steps, "ask", deleted_after_the_first_apply)
    await asyncio.wait_for(worker.wake(thread.id), 30.0)
    # Put back whole by its worker, though the journal refused its next apply before it heard: nothing is left in the folder.
    assert seen(here.folder) == before and kept(here) == {} and here.app.places.holder is None
    [row] = await records(api, thread)
    assert row.saga_state == "compensated" and await left_open(api, thread) == []
    [report] = await told(api, master)
    assert (report.data["landing"], report.data["landing_reason"]) == ("compensated", "deleted")


@pytest.mark.slow
async def test_a_thread_deleted_whose_interrupt_never_reaches_its_worker_has_its_landings_put_back_bounded_all_the_same(
    api, here, monkeypatch,
):
    heard_soon(monkeypatch)
    # Five minutes; here it is two seconds.
    monkeypatch.setattr(local_landing, "_PUT_BACK_BOUND", 2.0)
    monkeypatch.setattr(operations_module, "WAIT_GRACE_S", 0.1)
    lost_master, lost, _, next_ = await two_threads(api, monkeypatch, here)
    ask = Steps.ask

    async def deleted_after_the_first_apply(self, kind, action, **arguments):
        answer = await ask(self, kind, action, **arguments)
        if action == "apply" and arguments["path"] == "Budget.xlsx":
            # Its interrupt is lost: the worker learns of it from the journal's refusal of its next apply alone.
            assert (await api.client.delete(f"/v1/sessions/{lost.id}", headers=api.auth())).status_code == 204
            away_at(here, "unapply")
        return answer

    monkeypatch.setattr(Steps, "ask", deleted_after_the_first_apply)
    worker = a_worker(api, monkeypatch, lost, [EDITS, _final_response("Done.")])
    await asyncio.wait_for(worker.wake(lost.id), 10.0)
    # Its put-back met its computer gone: within the bound its worker ends, the landing left for the next in its folder.
    [row] = await records(api, lost)
    assert row.saga_state == "running"
    [report] = await told(api, lost_master)
    assert (report.data["landing"], report.data["landing_reason"]) == ("unsettled", "deleted")
    await back(here)
    await lands_next(api, monkeypatch, here, next_)
    [settled] = await records(api, lost)
    assert settled.saga_state == "compensated" and not (here.folder / "Budget.xlsx").exists() and kept(here) == {}


@pytest.mark.slow
@pytest.mark.parametrize("at", ["its put-back", "its look at its record"])
async def test_a_thread_deleted_while_its_landing_puts_itself_back_has_that_put_back_bounded_too(api, here, monkeypatch, at):
    heard_soon(monkeypatch)
    # Five minutes; here it is two seconds.
    monkeypatch.setattr(local_landing, "_PUT_BACK_BOUND", 2.0)
    lost_master, lost, _, next_ = await two_threads(api, monkeypatch, here)
    report_file = here.folder / "Report.docx"
    look = at == "its look at its record"

    def you_save_then_away(frame, outcome):
        action = frame["args"].get("action")
        if frame["invocation_id"] != "land:0":
            return outcome
        if action == "commit" and not look:
            # You save the report after the turn was committed: its apply is refused, and the landing puts itself back.
            report_file.write_bytes(b"PK report v2, by you")
        elif action == "record" and look:
            # Its record ran, and was answered with a refusal: the landing asks the folder's history whether it pushed.
            return {"error": {"type": "interrupted", "message": "This computer stopped while it ran"}}
        elif action == ("fetch" if look else "unapply"):
            here.app.reply = False
        return outcome

    monkeypatch.setattr(operations_module, "WAIT_GRACE_S", 0.1)
    here.app.lie = you_save_then_away
    worker = a_worker(api, monkeypatch, lost, [EDITS, _final_response("Done.")])
    turn = asyncio.create_task(worker.wake(lost.id))
    await eventually(lambda: gone(here), timeout=20.0)
    await delete(api, lost, worker)
    # Past the bound its worker ends there, the landing left for the next in its folder.
    await asyncio.wait_for(turn, 20.0)
    [row] = await records(api, lost)
    assert row.saga_state == "running"
    [report] = await told(api, lost_master)
    assert (report.data["landing"], report.data["landing_reason"]) == ("unsettled", "deleted")
    await back(here)
    await lands_next(api, monkeypatch, here, next_)
    [settled] = await records(api, lost)
    assert await left_open(api, lost) == [] and kept(here) == {}
    if look:
        # It had pushed: the next landing finds it so, and completes it with nothing put back.
        assert settled.saga_state == "completed" and (here.folder / "Budget.xlsx").read_text() == "Total,42\n"
    else:
        assert settled.saga_state == "compensated"
        assert report_file.read_bytes() == b"PK report v2, by you" and not (here.folder / "Budget.xlsx").exists()


@pytest.mark.slow
@pytest.mark.parametrize("at", ["the step in flight", "its put-back", "its forgetting"])
async def test_a_deleted_threads_landing_its_computer_does_not_end_in_time_is_left_for_the_next_landing_in_its_folder(
    api, here, monkeypatch, at,
):
    heard_soon(monkeypatch)
    # Five minutes; here it is two seconds.
    monkeypatch.setattr(local_landing, "_PUT_BACK_BOUND", 2.0)
    lost_master, lost, _, next_ = await two_threads(api, monkeypatch, here)
    before = seen(here.folder)
    turn, worker = await landing_away(api, monkeypatch, here, lost, "apply", "Report.docx")
    await delete(api, lost, worker)
    if at != "the step in flight":
        # Its computer answers the step in flight, and is gone again at the next step it runs.
        away_at(here, "unapply" if at == "its put-back" else "forget")
        here.app.reply = True
        await here.app.connect()
    # Past the bound its worker ends there: a landing it did not put back whole is left as its row says.
    await asyncio.wait_for(turn, 20.0)
    [row] = await records(api, lost)
    assert row.saga_state == ("compensated" if at == "its forgetting" else "running")
    [report] = await told(api, lost_master)
    assert (report.data["landing"], report.data["landing_reason"]) == (
        "compensated" if at == "its forgetting" else "unsettled", "deleted",
    )
    # Back, the next landing in the folder puts what is left of it back whole before it lands its own.
    await back(here)
    await lands_next(api, monkeypatch, here, next_)
    [settled] = await records(api, lost)
    assert settled.saga_state == "compensated" and await left_open(api, lost) == []
    assert {path: entry for path, entry in seen(here.folder).items() if path != "Y.md"} == before and kept(here) == {}


async def test_a_deleted_threads_journal_takes_only_the_steps_that_end_a_landing_it_had_begun_while_that_landing_is_not_over(api):
    device, _, thread = await bound_with_copy(api)
    store, ops = api.app.state.session_store, journal(api)
    saga = Saga(saga_id=f"saga:{uuid5(thread.id, 'land:3')}", session_id=thread.id, kind="landing")
    row = await start_landing(
        api.app.state.session_factory, saga, workstream_id=thread.config["workstream_id"], thread_id=thread.id,
        agent_id=str(thread.agent_id), user_id=thread.user_id, tool_saga_id=None, events=None, device_id=UUID(device["id"]),
        folder=FOLDER,
    )
    await store.update_session_status(thread.id, "archived")
    ending = [("land", "unapply"), ("land", "forget"), ("history", "fetch"), ("history", "record"), ("history", "forget")]
    # Under that landing's own name, what puts it back, records it or forgets it is taken, while it runs.
    for kind, action in ending:
        await recorded(api, asked(device, thread, kind, action, invocation="land:3"))
    # Nothing new: no write of the folder's, no step of a landing begun, no landing under another name, no turn.
    for kind, action, invocation in [
        ("land", "apply", "land:3"), ("land", "revisions", "land:3"), ("history", "commit", "land:3"),
        ("land", "unapply", "land:4"), ("land", "unapply", "land:3:settle:saga:x"), ("land", "forget", "land:3:release:1"),
        ("checkpoint", "restore", "checkpoint:3:restore:x"),
    ]:
        with pytest.raises(DeviceOperationError, match="This session was stopped"):
            await ops._record(asked(device, thread, kind, action, invocation=invocation))
    # Ended, it is still taken until it is forgotten; then nothing of it is.
    async with api.app.state.session_factory() as db:
        await db.execute(update(WorkstreamHistory).where(WorkstreamHistory.id == row).values(saga_state="compensated"))
        await db.commit()
    await recorded(api, OperationRequest(**{**fields_of(asked(device, thread, "land", "forget", invocation="land:3")), "ordinal": 50}))
    async with api.app.state.session_factory() as db:
        await db.execute(update(WorkstreamHistory).where(WorkstreamHistory.id == row).values(
            steps=[{"step_id": "f", "tool_name": "history.forget", "state": "committed", "arguments": {}}],
        ))
        await db.commit()
    with pytest.raises(DeviceOperationError, match="This session was stopped"):
        await ops._record(OperationRequest(**{**fields_of(asked(device, thread, "land", "forget", invocation="land:3")), "ordinal": 51}))


# -- a landing nobody runs -----------------------------------------------------------------------------------------


async def test_a_landing_left_half_done_that_nobody_resumes_has_its_thread_wait_on_you(api, here, monkeypatch):
    project, _, thread = await begun_with_copy(api, here)
    store = api.app.state.session_store
    counts = WorkstreamStore(api.app.state.session_factory).thread_counts
    with monkeypatch.context() as first_run:
        lost_after(first_run, "apply", 1)
        with pytest.raises(Lost):
            await woken(api, monkeypatch, thread, [EDITS, _final_response("Done.")])
    # Half landed: the budget is the thread's, your report as it was.
    assert (here.folder / "Budget.xlsx").read_text() == "Total,42\n" and (here.folder / "Report.docx").read_bytes() == b"PK report v1"
    # While a worker holds its turn, the landing is that worker's to finish: the thread works.
    held = await store.try_acquire_lease(thread.id, "a-worker-still-at-it", ttl_seconds=60)
    [row] = await rows(api, project)
    assert (row["group"], row["reason"]) == ("working", None)
    assert (await counts([UUID(project["id"])]))[UUID(project["id"])][1:] == (0, 1)
    # Its worker is gone, and nothing resumes a turn that had given its answer: one file of two is in your folder.
    await store.release_lease(thread.id, held.lease_token)
    [row] = await rows(api, project)
    assert (row["group"], row["reason"], row["status_line"]) == ("waiting", "files", "Couldn't finish landing my changes")
    assert (await counts([UUID(project["id"])]))[UUID(project["id"])][1:] == (1, 0)
    # A message to the thread takes its turn up again, whose end goes on with the landing; then it waits on you for nothing.
    await woken(api, monkeypatch, thread, [_final_response("Done.")], said="Go on.")
    [row] = await rows(api, project)
    assert (row["group"], row["reason"]) == ("idle", None)
    assert (here.folder / "Budget.xlsx").read_text() == "Total,42\n" and (here.folder / "Report.docx").read_bytes() == b"PK report v1 edited"
