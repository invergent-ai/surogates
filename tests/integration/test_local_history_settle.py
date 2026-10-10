"""A landing on the user's computer left running is settled before anything else lands in its folder; a worker without its lease stands down.

Each scene runs a worker's real wake on the tests' computer.  A worker is
lost as a killed one is: raised out of its turn by what no ``except
Exception`` catches, or cancelled while it waits as a turn handed to another
worker is, which closes nothing of its own.  The next landing in the folder,
another project's thread's, then settles what it left: completed where it
recorded, put back whole where it did not.  The scenes that write the folder
run twice: on the tests' computer's land rules, and on the app's own file
helper in their place (marked ``desktop``).  The folder is read back by name,
mode, time and bytes.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid5

import pytest
from sqlalchemy import select, text

import surogates.devices.operations as operations_module
from surogates.db.models import WorkstreamHistory
from surogates.devices.history import Steps
from surogates.governance.saga import Saga, SagaStep
from surogates.harness import landing as landing_module
from surogates.harness import local_landing
from surogates.harness.local_landing import land_local_turn
from surogates.session.events import EventType
from surogates.workstreams.history import start_landing
from tests.test_fake_places import built_helper  # noqa: F401  (a fixture)
from tests.test_steer_loop import _final_response

from .test_devices import FOLDER, api, eventually, link_url, register  # noqa: F401  (api and link_url are fixtures)
from .test_local_history_landing import EDITS, LANDED, files_of, here, holding, kept, lie_at, records, said, seen, told  # noqa: F401
from .test_local_history_open import begun_with_copy, woken
from .test_local_history_threads import computer, made_with_copy, tool  # noqa: F401  (computer is a fixture)
from .test_turn_sagas import calling
from .test_workstream_threads import events_of

pytestmark = pytest.mark.asyncio(loop_scope="session")


class Lost(BaseException):
    """A worker lost: nothing of its turn runs on, not even what cleans up after an error."""


def lost_after(monkeypatch, action: str, answered: int) -> None:
    """The worker is lost once its computer has answered *action* that many times, its answers from the journal among them."""
    ask, count = Steps.ask, [0]

    async def asking(self, kind, asked, **arguments):
        answer = await ask(self, kind, asked, **arguments)
        if asked == action:
            count[0] += 1
            if count[0] == answered:
                raise Lost
        return answer

    monkeypatch.setattr(Steps, "ask", asking)


SECOND, THIRD = "second-binding-nonce-0001", "third-binding-nonce-00001"
#: The next thread's turn: one file of its own, which lands in the folder after the lost landing is settled.
NEXT = [calling(("write_file", {"path": "Y.md", "content": "y\n"})), _final_response("Done.")]


async def two_threads(api, monkeypatch, here):
    """Two projects' threads on one folder of *here*'s, which has landed before: the one whose landing is lost, and the next to land there.

    With their masters.  A third thread landed first: the folder's history
    has a ``main``, as it has after any landing there.
    """
    _, _, first = await begun_with_copy(api, here, nonce=THIRD)
    await woken(api, monkeypatch, first, [calling(("write_file", {"path": "Plans/Q4.md", "content": "Q4 plan\n"})), _final_response("Done.")])
    _, lost_master, lost = await begun_with_copy(api, here)
    _, next_master, next_ = await begun_with_copy(api, here, nonce=SECOND)
    return lost_master, lost, next_master, next_


def asked_by(here, session, invocation: str) -> list[str]:
    """What *session* itself asked *here*'s app under *invocation*: two threads' first turns have one name for theirs."""
    return [action for under, calling_, _, action in here.app.places.asked if (under, calling_) == (invocation, str(session.id))]


def saga_of_turn(thread, invocation: str = "land:0") -> str:
    return f"saga:{uuid5(thread.id, invocation)}"


def settles(thread) -> str:
    """The invocation the next thread's first turn settles *thread*'s first landing under."""
    return f"land:0:settle:{saga_of_turn(thread)}"


async def lands_next(api, monkeypatch, here, next_, replies=NEXT, **more):
    """The next thread's turn, once the app let the folder go: the lost landing's helper was idle two minutes."""
    here.app.places.holder = None
    return await woken(api, monkeypatch, next_, replies, **more)


async def left_open(api, session) -> list[tuple[str, str]]:
    """The operations of *session* its computer has not answered and nobody closed, as (invocation, action)."""
    async with api.app.state.session_factory() as db:
        rows = await db.execute(text(
            "SELECT invocation_id, args->>'action' FROM device_operations"
            " WHERE calling_session_id = :id AND completed_at IS NULL ORDER BY created_at, ordinal"
        ), {"id": session.id})
        return [tuple(row) for row in rows]


def but(entries: dict, *names: str) -> dict:
    return {path: entry for path, entry in entries.items() if path not in names}


async def killed_while_it_waits(api, monkeypatch, here, thread, held) -> list[dict]:
    """*thread*'s turn, its worker killed while it waits for the first operation *held* names; the frames its app took and ran none of.

    Killed, it closes nothing: as a turn handed to another worker, whose
    operation stays open in the journal for its computer to run when back.
    """
    frames = holding(here, held)
    turn = asyncio.create_task(woken(api, monkeypatch, thread, [EDITS, _final_response("Done.")]))

    async def waits() -> bool:
        return bool(frames)

    await eventually(waits, timeout=20.0)
    with monkeypatch.context() as dying:
        dying.setattr(operations_module, "turn_detached", lambda: True)
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
    # Its app takes what comes from here on as it would.
    del here.app._handle
    return frames


async def back_again(here) -> None:
    """*here*'s computer drops its link and comes back: the server sends it every operation still open."""
    await here.app.disconnect()
    await here.app.connect()
    await asyncio.sleep(1.0)


# -- a landing nobody resumes, settled by the next in its folder ----------------------------------------------------


@pytest.mark.parametrize("cut", ["after its look", "after an apply", "in its put-back"])
async def test_a_landing_lost_before_it_recorded_is_put_back_whole_by_the_next_landing_in_its_folder_before_that_one_looks(
    api, here, monkeypatch, cut,
):
    lost_master, lost, next_master, next_ = await two_threads(api, monkeypatch, here)
    before = seen(here.folder)
    yours: dict = {}
    if cut == "in its put-back":
        # You save the report after its turn was committed: its apply is refused, and the landing goes back, newest first.
        def you_save(frame, outcome) -> None:
            (here.folder / "Report.docx").write_bytes(b"PK report v2, by you")
            yours.update(seen(here.folder))

        lie_at(here, "commit", you_save)
    with monkeypatch.context() as first_run:
        lost_after(first_run, *{"after its look": ("revisions", 1), "after an apply": ("apply", 1), "in its put-back": ("unapply", 1)}[cut])
        with pytest.raises(Lost):
            await woken(api, monkeypatch, lost, [EDITS, _final_response("Done.")])
    here.app.lie = None
    [row] = await records(api, lost)
    assert row.saga_state == "running"
    if cut != "after its look":
        # Half landed: the budget is the lost thread's.
        assert (here.folder / "Budget.xlsx").read_text() == "Total,42\n"
    await lands_next(api, monkeypatch, here, next_)
    # Put back whole before the next landing looked at the folder, then forgotten; then the next landed its own.
    [settled] = await records(api, lost)
    assert settled.saga_state == "compensated"
    unapplies = [] if cut == "after its look" else ["unapply", "unapply"]
    assert asked_by(here, next_, settles(lost)) == [*unapplies, "forget"]
    assert asked_by(here, next_, "land:0") == LANDED[:4] + ["apply", "record", "forget"]
    # Every entry of the folder is as it was, entry for entry, but your save and the next thread's file.
    after = seen(here.folder)
    assert but(after, "Y.md", "Report.docx") == but(before, "Report.docx")
    assert after["Report.docx"] == (yours or before)["Report.docx"]
    assert (here.folder / "Y.md").read_text() == "y\n" and kept(here) == {} and here.app.places.holder is None
    [own] = await records(api, next_)
    assert (own.saga_state, [f["path"] for f in own.files]) == ("completed", ["Y.md"])
    # Nobody waits on you over the lost landing: nothing of it is left in the folder.
    assert await events_of(api, lost.id, EventType.INBOX_ACTION_REQUIRED) == []
    assert await left_open(api, lost) == []


@pytest.mark.parametrize("at", ["its first step", "an apply", "its record, before it pushed"])
async def test_what_a_worker_killed_while_it_waited_left_open_is_closed_before_anything_goes_back_and_never_runs(api, here, monkeypatch, at):
    lost_master, lost, next_master, next_ = await two_threads(api, monkeypatch, here)
    before = seen(here.folder)
    action = {"its first step": "changed", "an apply": "apply", "its record, before it pushed": "record"}[at]
    frames = await killed_while_it_waits(
        api, monkeypatch, here, lost,
        lambda frame: (frame["session_id"], frame["invocation_id"], frame["args"].get("action")) == (str(lost.id), "land:0", action),
    )
    assert [frame["args"]["action"] for frame in frames] == [action]
    assert (await left_open(api, lost))[-1] == ("land:0", action)
    await lands_next(api, monkeypatch, here, next_)
    # Closed first: then the history was asked where the journal held a record, and every apply went back.
    assert await left_open(api, lost) == []
    expected = {"changed": ["forget"], "apply": ["unapply", "unapply", "forget"], "record": ["fetch", "unapply", "unapply", "forget"]}
    assert asked_by(here, next_, settles(lost)) == expected[action]
    [settled] = await records(api, lost)
    assert settled.saga_state == "compensated"
    # Its computer back, nothing of the lost landing runs: the folder is as the settle left it, entry for entry.
    landed = seen(here.folder)
    await back_again(here)
    assert seen(here.folder) == landed and but(landed, "Y.md") == before
    assert kept(here) == {}


@pytest.mark.parametrize("cut", ["its answer read, its row not written", "its answer lost with its link"])
async def test_a_landing_lost_once_its_record_pushed_is_completed_by_the_next_landing_and_nothing_of_it_is_put_back(api, here, monkeypatch, cut):
    lost_master, lost, next_master, next_ = await two_threads(api, monkeypatch, here)
    if cut == "its answer read, its row not written":
        with monkeypatch.context() as first_run:
            lost_after(first_run, "record", 1)
            with pytest.raises(Lost):
                await woken(api, monkeypatch, lost, [EDITS, _final_response("Done.")])
    else:
        def gone_at_the_record(frame, outcome):
            if frame["args"].get("action") == "record":
                # It recorded, and its link dropped before the answer was sent.
                here.app.reply = False
            return outcome

        here.app.lie = gone_at_the_record
        turn = asyncio.create_task(woken(api, monkeypatch, lost, [EDITS, _final_response("Done.")]))

        async def gone() -> bool:
            return not here.app.connected and (await left_open(api, lost))[-1:] == [("land:0", "record")]

        await eventually(gone, timeout=20.0)
        with monkeypatch.context() as dying:
            dying.setattr(operations_module, "turn_detached", lambda: True)
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
        here.app.lie, here.app.reply = None, True
        await here.app.connect()
    assert (await records(api, lost))[0].saga_state == "running"
    landed = seen(here.folder)
    await lands_next(api, monkeypatch, here, next_)
    # The folder's history holds it: completed with its commit, asked only to be forgotten, and its files stay.
    [settled] = await records(api, lost)
    assert settled.saga_state == "completed" and len(settled.commit) == 40
    assert sorted((f["path"], f["merged"]) for f in settled.files) == [("Budget.xlsx", True), ("Report.docx", True)]
    assert asked_by(here, next_, settles(lost)) == ["fetch", "forget"]
    assert but(seen(here.folder), "Y.md") == landed and kept(here) == {}
    assert (here.folder / "Budget.xlsx").read_text() == "Total,42\n" and (here.folder / "Report.docx").read_bytes() == b"PK report v1 edited"
    [own] = await records(api, next_)
    # Its files are the landing's, not yours: the next landing picked up nothing.
    assert (own.saga_state, [f["path"] for f in own.files], own.picked_up) == ("completed", ["Y.md"], [])


@pytest.mark.parametrize("cut", ["before its record", "after its record"])
async def test_a_landing_whose_row_a_turn_taken_up_again_wrote_short_is_settled_by_what_its_computer_was_asked(api, here, monkeypatch, cut):
    lost_master, lost, next_master, next_ = await two_threads(api, monkeypatch, here)
    before = seen(here.folder)
    with monkeypatch.context() as first_run:
        lost_after(first_run, *({"before its record": ("apply", 2), "after its record": ("record", 1)}[cut]))
        with pytest.raises(Lost):
            await woken(api, monkeypatch, lost, [EDITS, _final_response("Done.")])
    landed = seen(here.folder)
    with monkeypatch.context() as again:
        # Taken up again, its turn writes the steps it has asked again so far, and is lost once more early on.
        again.setattr(landing_module, "_ROW_EVERY", 0)
        lost_after(again, "commit", 1)
        with pytest.raises(Lost):
            await woken(api, monkeypatch, lost, [_final_response("Done.")])
    # Its row names neither an apply nor its record now.
    [row] = await records(api, lost)
    assert {s["tool_name"] for s in row.steps} <= {"history.pickup", "history.commit"}
    await lands_next(api, monkeypatch, here, next_)
    [settled] = await records(api, lost)
    if cut == "before its record":
        # Every apply its computer was asked goes back, though its row no longer names one.
        assert settled.saga_state == "compensated"
        assert asked_by(here, next_, settles(lost)) == ["unapply", "unapply", "forget"]
        assert but(seen(here.folder), "Y.md") == before
    else:
        # Its record is in the journal, though not in its row: the folder's history is asked, and it stands, complete.
        assert settled.saga_state == "completed"
        assert sorted((f["path"], f["merged"]) for f in settled.files) == [("Budget.xlsx", True), ("Report.docx", True)]
        assert asked_by(here, next_, settles(lost)) == ["fetch", "forget"]
        assert but(seen(here.folder), "Y.md") == landed
    assert kept(here) == {}


async def test_a_settle_whose_own_worker_is_lost_in_its_put_back_goes_on_with_it_when_its_turn_resumes(api, here, monkeypatch):
    (here.folder / "Budget.xlsx").write_text("Total,1\n")
    lost_master, lost, next_master, next_ = await two_threads(api, monkeypatch, here)
    before = seen(here.folder)
    four = calling(("terminal", {"command": "echo Total,42 > Budget.xlsx && echo b > B.md && echo c > C.md && echo d > D.md"}))
    with monkeypatch.context() as first_run:
        # Four files: each applied, the fourth answered as its worker is lost.
        lost_after(first_run, "apply", 4)
        with pytest.raises(Lost):
            await woken(api, monkeypatch, lost, [four, _final_response("Done.")])
    assert sorted(p.name for p in here.folder.iterdir()) == ["B.md", "Budget.xlsx", "C.md", "D.md", "Plans", "Report.docx"]
    replies = NEXT
    # The settling thread's worker is lost twice in the middle of the put-back, and its turn resumed each time.
    for answered in (3, 4):
        with monkeypatch.context() as cut_short:
            lost_after(cut_short, "unapply", answered)
            with pytest.raises(Lost):
                await lands_next(api, monkeypatch, here, next_, replies)
        replies = [_final_response("Done.")]
    await lands_next(api, monkeypatch, here, next_, replies)
    # Each put-back was asked once, in one order however often the settle began again.
    [settled] = await records(api, lost)
    assert settled.saga_state == "compensated"
    assert asked_by(here, next_, settles(lost)) == ["unapply", "unapply", "unapply", "unapply", "forget"]
    # Newest first: each by its step, its place among the turn's changes.
    async with api.app.state.session_factory() as db:
        asked = (await db.execute(text(
            "SELECT args->>'path' FROM device_operations WHERE calling_session_id = :id AND invocation_id = :at"
            " AND args->>'action' = 'unapply' ORDER BY ordinal"
        ), {"id": next_.id, "at": settles(lost)})).scalars().all()
    assert asked == ["D.md", "C.md", "Budget.xlsx", "B.md"]
    assert but(seen(here.folder), "Y.md") == before and kept(here) == {}
    assert await events_of(api, lost.id, EventType.INBOX_ACTION_REQUIRED) == []
    [own] = await records(api, next_)
    assert (own.saga_state, [f["path"] for f in own.files]) == ("completed", ["Y.md"])


async def test_a_landing_a_settle_began_on_is_settled_by_its_own_turn_taken_up_again_never_gone_on_with(api, here, monkeypatch):
    lost_master, lost, next_master, next_ = await two_threads(api, monkeypatch, here)
    before = seen(here.folder)
    with monkeypatch.context() as first_run:
        lost_after(first_run, "apply", 2)
        with pytest.raises(Lost):
            await woken(api, monkeypatch, lost, [EDITS, _final_response("Done.")])
    unapplied = [0]

    def restarted(frame, outcome):
        if (frame["invocation_id"], frame["args"].get("action")) == (settles(lost), "unapply"):
            unapplied[0] += 1
            if unapplied[0] == 2:
                # Its app restarted as it answered the second put-back: the file is back, and the answer says it was cut off.
                return {"error": {"type": "interrupted", "message": "This computer's app stopped while it ran"}}
        return outcome

    here.app.lie = restarted
    await lands_next(api, monkeypatch, here, next_)
    here.app.lie = None
    # The settle stopped there: the next thread landed nothing, and the lost landing is left running, put back.
    [report] = await told(api, next_master)
    assert (report.data["landing"], report.data["landing_reason"], report.data["landing_code"]) == ("compensated", "refused", "interrupted")
    assert (await records(api, lost))[0].saga_state == "running" and but(seen(here.folder), "Y.md") == before
    # Its own turn is taken up again, its model answering with no step: the landing a settle began is settled, never gone on with.
    here.app.places.holder = None
    await woken(api, monkeypatch, lost, [_final_response("Done.")], said="Is it done?")
    assert asked_by(here, lost, "land:0") == ["changed", "revisions", "pickup", "commit", "apply", "apply"]
    assert asked_by(here, lost, settles(lost)) == ["unapply", "unapply", "forget"]
    first, again = await records(api, lost)
    assert (first.saga_state, again.saga_state, again.saga_id) == ("compensated", "completed", saga_of_turn(lost, "land:0:2"))
    # Then the turn landed again, as a new saga: its files are in the folder, as its report and its record say.
    assert (here.folder / "Budget.xlsx").read_text() == "Total,42\n" and (here.folder / "Report.docx").read_bytes() == b"PK report v1 edited"
    assert sorted((f["path"], f["merged"]) for f in again.files) == [("Budget.xlsx", True), ("Report.docx", True)]
    [report] = await told(api, lost_master)
    assert files_of(report) == [("Budget.xlsx", "landed", None), ("Report.docx", "landed", None)] and kept(here) == {}


async def test_an_earlier_turns_landing_of_the_settling_thread_is_settled_under_its_own_lease(api, here, monkeypatch):
    monkeypatch.setattr(local_landing, "BUSY_WAIT", 0.2)
    monkeypatch.setattr(local_landing, "BUSY_PATIENCE", 3.0)
    _, master, thread = await begun_with_copy(api, here)
    before = seen(here.folder)
    with monkeypatch.context() as away:
        # Its record is never answered: the landing is left unsettled, and its turn ends.
        away.setattr(local_landing, "STEP_WAIT", 2)
        frames = holding(here, lambda frame: frame["kind"] == "history" and frame["args"].get("action") == "record")
        await woken(api, monkeypatch, thread, [EDITS, _final_response("Done.")])
    del here.app._handle
    assert [frame["invocation_id"] for frame in frames] == ["land:0"]
    [end] = await events_of(api, thread.id, EventType.SESSION_COMPLETE)
    # Its next turn holds its own lease: it settles that landing first, from its record, then lands.
    await woken(api, monkeypatch, thread, NEXT, said="Go on.")
    assert asked_by(here, thread, f"land:{end.id}:settle:{saga_of_turn(thread)}") == ["fetch", "unapply", "unapply", "forget"]
    first, second = await records(api, thread)
    assert (first.saga_state, second.saga_state) == ("compensated", "completed")
    assert sorted(f["path"] for f in second.files) == ["Budget.xlsx", "Report.docx", "Y.md"]
    assert but(seen(here.folder), "Budget.xlsx", "Report.docx", "Y.md") == but(before, "Report.docx")
    assert (here.folder / "Report.docx").read_bytes() == b"PK report v1 edited" and kept(here) == {}


async def test_a_landing_does_not_land_over_one_left_half_done_whose_turn_a_worker_still_holds(api, here, monkeypatch):
    lost_master, lost, next_master, next_ = await two_threads(api, monkeypatch, here)
    before = seen(here.folder)
    with monkeypatch.context() as first_run:
        lost_after(first_run, "apply", 1)
        with pytest.raises(Lost):
            await woken(api, monkeypatch, lost, [EDITS, _final_response("Done.")])
    # A worker holds the lost landing's turn still: it is that worker's to finish.
    store = api.app.state.session_store
    still = await store.try_acquire_lease(lost.id, "worker-still-there", ttl_seconds=120)
    assert still is not None
    monkeypatch.setattr(local_landing, "BUSY_WAIT", 0.2)
    here.app.places.holder = None
    waiting = asyncio.create_task(woken(api, monkeypatch, next_, NEXT))

    async def gave_the_folder_back() -> bool:
        return asked_by(here, next_, "land:0:hold").count("forget") >= 2

    # It takes the folder, finds the half landing that worker holds, and gives the folder back to wait for it.
    await eventually(gave_the_folder_back, timeout=20.0)
    assert not waiting.done() and asked_by(here, next_, "land:0") == []
    assert (here.folder / "Budget.xlsx").read_text() == "Total,42\n" and (await records(api, lost))[0].saga_state == "running"
    # That worker is gone for good: the waiting landing takes its turn's lease, puts the half landing back, then lands.
    await store.release_lease(lost.id, still.lease_token)
    await asyncio.wait_for(waiting, 30.0)
    [settled], [own] = await records(api, lost), await records(api, next_)
    assert (settled.saga_state, own.saga_state, own.picked_up) == ("compensated", "completed", [])
    assert but(seen(here.folder), "Y.md") == before and kept(here) == {}
    # The lease it took is given back: the lost thread's next turn is nobody's but its own.
    async with api.app.state.session_factory() as db:
        assert (await db.execute(text("SELECT count(*) FROM session_leases WHERE session_id = :id"), {"id": lost.id})).scalar() == 0


def lost_before(monkeypatch, thread, action: str, invocation: str = "land:0") -> None:
    """*thread*'s worker is lost just before it would ask *action* under *invocation*."""
    ask = Steps.ask

    async def asking(self, kind, asked, **arguments):
        runner = self._runner
        if (asked, runner._invocation_id, runner._calling_session_id) == (action, invocation, thread.id):
            raise Lost
        return await ask(self, kind, asked, **arguments)

    monkeypatch.setattr(Steps, "ask", asking)


@pytest.mark.parametrize("ended", ["recorded", "put back whole"])
async def test_a_landing_whose_own_forgetting_never_ran_is_forgotten_by_the_next_landing_in_its_folder(api, here, monkeypatch, ended):
    lost_master, lost, next_master, next_ = await two_threads(api, monkeypatch, here)
    if ended == "put back whole":
        # You save the report after its turn was committed: its apply is refused, and the landing goes back whole.
        lie_at(here, "commit", lambda frame, outcome: (here.folder / "Report.docx").write_bytes(b"PK report v2, by you") and None)
    with monkeypatch.context() as first_run:
        lost_before(first_run, lost, "forget")
        with pytest.raises(Lost):
            await woken(api, monkeypatch, lost, [EDITS, _final_response("Done.")])
    here.app.lie = None
    [row] = await records(api, lost)
    assert row.saga_state == ("completed" if ended == "recorded" else "compensated") and row.steps[-1]["tool_name"] != "history.forget"
    saga = saga_of_turn(lost)
    assert kept(here) == ({f"{saga}/1": (0o600, b"PK report v1")} if ended == "recorded" else {})
    landed = seen(here.folder)
    await lands_next(api, monkeypatch, here, next_)
    # The next landing in the folder asks it again, once it holds the folder, under a settle's name: what was kept goes.
    assert asked_by(here, next_, f"{settles(lost)}:forget") == ["forget"] and kept(here) == {}
    [row] = await records(api, lost)
    assert (row.steps[-1]["tool_name"], row.steps[-1]["state"]) == ("history.forget", "committed")
    applied = row.steps[-1]["arguments"]["applied"]
    # Recorded, it names every apply; put back whole, it keeps nothing, and names none.
    assert [a["path"] for a in applied] == (["Budget.xlsx", "Report.docx"] if ended == "recorded" else [])
    assert but(seen(here.folder), "Y.md") == landed
    [own] = await records(api, next_)
    assert own.saga_state == "completed"
    # Asked once: the landing after finds nothing owed.
    asked = len(here.app.places.asked)
    await woken(api, monkeypatch, next_, [calling(("write_file", {"path": "Z.md", "content": "z\n"})), _final_response("Done.")], said="More.")
    assert "forget" not in [action for under, _, _, action in here.app.places.asked[asked:] if ":settle:" in under]


# -- what a settle cannot finish -------------------------------------------------------------------------------------


async def test_a_lost_landing_that_pushed_and_whose_row_cannot_say_so_stops_every_landing_in_its_folder_until_it_can(api, here, monkeypatch):
    lost_master, lost, next_master, next_ = await two_threads(api, monkeypatch, here)
    with monkeypatch.context() as first_run:
        lost_after(first_run, "record", 1)
        with pytest.raises(Lost):
            await woken(api, monkeypatch, lost, [EDITS, _final_response("Done.")])
    [row] = await records(api, lost)
    landed = seen(here.folder)
    save = landing_module.save_landing

    async def unwritable(session_factory, row_id, saga, **values):
        if row_id == row.id and values.get("state") == "completed":
            raise ConnectionError("the database went away")
        return await save(session_factory, row_id, saga, **values)

    with monkeypatch.context() as down:
        down.setattr(landing_module, "save_landing", unwritable)
        await lands_next(api, monkeypatch, here, next_)
    # The settle found it pushed, and could not write that down: nothing more lands in the folder, and the folder is let go.
    assert asked_by(here, next_, settles(lost)) == ["fetch"] and asked_by(here, next_, "land:0") == []
    assert (await records(api, lost))[0].saga_state == "running" and await records(api, next_) == []
    assert but(seen(here.folder), "Y.md") == landed and not (here.folder / "Y.md").exists() and here.app.places.holder is None
    [report] = await told(api, next_master)
    assert (report.data["landing"], report.data["landing_reason"]) == ("compensated", "settling")
    assert "nothing lands there until it can" in said(report) and "in its copy on its computer" in said(report)
    [end] = await events_of(api, next_.id, EventType.SESSION_COMPLETE)
    assert end.data["saved"] is False
    # Written once the database is back: the next landing in the folder settles it, and lands.
    ended = end.id
    await lands_next(api, monkeypatch, here, next_, [_final_response("Done.")], said="Land it now.")
    assert asked_by(here, next_, f"land:{ended}:settle:{saga_of_turn(lost)}") == ["fetch", "forget"]
    [settled] = await records(api, lost)
    assert settled.saga_state == "completed" and sorted(f["path"] for f in settled.files) == ["Budget.xlsx", "Report.docx"]
    assert (here.folder / "Y.md").read_text() == "y\n" and kept(here) == {}


async def test_a_lost_landing_the_next_cannot_put_back_whole_is_left_escalated_and_its_thread_waits_on_you(api, here, monkeypatch):
    lost_master, lost, next_master, next_ = await two_threads(api, monkeypatch, here)
    with monkeypatch.context() as first_run:
        lost_after(first_run, "apply", 2)
        with pytest.raises(Lost):
            await woken(api, monkeypatch, lost, [EDITS, _final_response("Done.")])
    # You save the report again, over the lost landing's: its put-back leaves yours.
    (here.folder / "Report.docx").write_bytes(b"PK report v3, by you")
    yours = seen(here.folder)["Report.docx"]
    await lands_next(api, monkeypatch, here, next_)
    [settled] = await records(api, lost)
    assert settled.saga_state == "escalated"
    # The budget went back; your report stands; what the landing replaced is kept, and nothing of it was forgotten.
    assert asked_by(here, next_, settles(lost)) == ["unapply", "unapply"]
    assert not (here.folder / "Budget.xlsx").exists() and seen(here.folder)["Report.docx"] == yours
    assert kept(here) == {f"{saga_of_turn(lost)}/1": (0o600, b"PK report v1")}
    # Its thread waits on you, in words that say where each thing is, and its master hears it, once.
    [waits] = await events_of(api, lost.id, EventType.INBOX_ACTION_REQUIRED)
    words = waits.data["instructions"]
    assert (waits.data["title"], waits.data["files"], waits.data["escalated"]) == ("Couldn't finish landing my changes", ["Report.docx"], True)
    assert FOLDER in words and "Flavius's ThinkPad" in words and "History" not in words
    [report] = await told(api, lost_master)
    assert (report.data["landing"], report.data["recovered"], report.data["saga"]) == ("escalated", True, settled.saga_id)
    assert files_of(report) == [("Report.docx", "not_merged", None)]
    assert "a landing its worker left unfinished was settled" in said(report) and "check them: Report.docx" in said(report)
    # The next landing went on: your report is yours, picked up as such.
    [own] = await records(api, next_)
    assert own.saga_state == "completed" and [p["path"] for p in own.picked_up] == ["Report.docx"]


async def test_a_lost_landing_whose_push_a_pruning_hides_is_given_up_with_nothing_put_back(api, here, monkeypatch):
    lost_master, lost, next_master, next_ = await two_threads(api, monkeypatch, here)
    with monkeypatch.context() as first_run:
        lost_after(first_run, "record", 1)
        with pytest.raises(Lost):
            await woken(api, monkeypatch, lost, [EDITS, _final_response("Done.")])
    landed = seen(here.folder)
    lie_at(here, "fetch", lambda frame, outcome: {"ok": {**outcome["ok"], "landing": None, "hidden": True}}, invocation=settles(lost))
    await lands_next(api, monkeypatch, here, next_)
    here.app.lie = None
    [settled] = await records(api, lost)
    assert settled.saga_state == "escalated" and asked_by(here, next_, settles(lost)) == ["fetch"]
    # Whether it pushed is not known: its files stay as they are, and what it replaced stays kept.
    assert but(seen(here.folder), "Y.md") == landed and kept(here) == {f"{saga_of_turn(lost)}/1": (0o600, b"PK report v1")}
    [waits] = await events_of(api, lost.id, EventType.INBOX_ACTION_REQUIRED)
    assert waits.data["files"] == ["Budget.xlsx", "Report.docx"]


# -- the worker's own turn, resumed --------------------------------------------------------------------------------


async def test_a_turn_resumed_after_its_worker_was_lost_mid_landing_gets_each_steps_recorded_outcome(api, here, monkeypatch):
    _, master, thread = await begun_with_copy(api, here)
    with monkeypatch.context() as first_run:
        lost_after(first_run, "apply", 1)
        with pytest.raises(Lost):
            await woken(api, monkeypatch, thread, [EDITS, _final_response("Done.")])
    await woken(api, monkeypatch, thread, [_final_response("Done.")])
    # Each step its computer had answered was answered again from the journal: nothing was applied twice.
    assert asked_by(here, thread, "land:0") == LANDED
    [row] = await records(api, thread)
    assert (row.saga_state, kept(here)) == ("completed", {})
    [report] = await told(api, master)
    assert files_of(report) == [("Budget.xlsx", "landed", None), ("Report.docx", "landed", None)]


async def test_a_turns_end_taken_up_again_goes_on_with_its_landing_as_its_first_run_began_it(api, computer, monkeypatch):
    _, _, thread = await begun_with_copy(api, computer)
    store = api.app.state.session_store
    assert not (await tool(api, thread, "terminal", command="echo Total,42 > Budget.xlsx && printf ' edited' >> Report.docx")).get("error")
    lease = await store.try_acquire_lease(thread.id, "worker-local", ttl_seconds=60)

    async def lands(tool_saga_id: str) -> dict:
        return await land_local_turn(
            store=store, session_factory=api.app.state.session_factory, redis=api.app.state.redis,
            session=await store.get_session(thread.id), lease_token=str(lease.lease_token), saga_settings=None,
            tool_saga_id=tool_saga_id,
        )

    with monkeypatch.context() as first_run:
        lost_after(first_run, "apply", 1)
        with pytest.raises(Lost):
            await lands("saga:the-first-runs")
    # Taken up again, its turn's tool saga is another's: the landing goes on as its first run began it, from the journal.
    landed = await lands("saga:the-next-runs")
    assert landed["state"] == "completed" and asked_by(computer, thread, "land:0") == LANDED
    [row] = await records(api, thread)
    assert (row.saga_state, row.tool_saga_id) == ("completed", "saga:the-first-runs")
    await store.release_lease(thread.id, lease.lease_token)


async def test_a_landing_resumed_on_another_path_is_settled_from_what_it_asked_and_lands_again(api, here, monkeypatch):
    _, master, thread = await begun_with_copy(api, here)
    with monkeypatch.context() as first_run:
        lost_after(first_run, "apply", 1)
        with pytest.raises(Lost):
            await woken(api, monkeypatch, thread, [EDITS, _final_response("Done.")])
    # Its title changed meanwhile: the resumed landing would commit as another author than the run it resumes.
    await api.app.state.session_store.update_session_title(thread.id, "Check every total")
    await woken(api, monkeypatch, thread, [_final_response("Done.")])
    first, second = await records(api, thread)
    assert (first.saga_state, second.saga_state) == ("compensated", "completed")
    assert asked_by(here, thread, f"land:0:settle:{first.saga_id}") == ["unapply", "unapply", "forget"]
    assert asked_by(here, thread, "land:0:2") == LANDED
    assert (here.folder / "Budget.xlsx").read_text() == "Total,42\n" and (here.folder / "Report.docx").read_bytes() == b"PK report v1 edited"
    assert kept(here) == {} and here.app.places.holder is None
    [report] = await told(api, master)
    assert files_of(report) == [("Budget.xlsx", "landed", None), ("Report.docx", "landed", None)]


@pytest.mark.parametrize("cut", ["before its record", "after its record"])
async def test_a_resumed_turn_that_works_on_lands_what_it_did_after_its_landing_began(api, here, monkeypatch, cut):
    _, master, thread = await begun_with_copy(api, here)
    with monkeypatch.context() as first_run:
        lost_after(first_run, *({"before its record": ("apply", 1), "after its record": ("record", 1)}[cut]))
        with pytest.raises(Lost):
            await woken(api, monkeypatch, thread, [EDITS, _final_response("Done.")])
    # Resumed on another worker, the turn takes one more step before it ends: a file its landing never named.
    await woken(api, monkeypatch, thread, [calling(("write_file", {"path": "More.md", "content": "more\n"})), _final_response("Done.")])
    assert (here.folder / "More.md").read_text() == "more\n"
    assert (here.folder / "Budget.xlsx").read_text() == "Total,42\n" and (here.folder / "Report.docx").read_bytes() == b"PK report v1 edited"
    first, more = await records(api, thread)
    if cut == "before its record":
        # Never recorded, its first landing went back whole, from what it had asked; the turn then landed whole, as a new saga.
        assert (first.saga_state, more.saga_state, more.saga_id) == ("compensated", "completed", saga_of_turn(thread, "land:0:2"))
        assert sorted(f["path"] for f in more.files) == ["Budget.xlsx", "More.md", "Report.docx"]
    else:
        # Recorded, its first landing stands; what the turn did since landed after it, as a landing of its own.
        assert (first.saga_state, more.saga_state, more.saga_id) == ("completed", "completed", saga_of_turn(thread, "land:0:3"))
        assert [f["path"] for f in more.files] == ["More.md"]
    [report] = await told(api, master)
    assert files_of(report) == [("Budget.xlsx", "landed", None), ("More.md", "landed", None), ("Report.docx", "landed", None)]
    [end] = await events_of(api, thread.id, EventType.SESSION_COMPLETE)
    assert end.data["saved"] is True and kept(here) == {}


# -- a worker without its lease ------------------------------------------------------------------------------------


async def test_a_worker_that_lost_its_lease_stands_down_and_the_one_that_holds_it_goes_on_with_the_landing(api, here, monkeypatch):
    _, master, thread = await begun_with_copy(api, here)
    ask, applied = Steps.ask, [0]

    async def asking(self, kind, action, **arguments):
        answer = await ask(self, kind, action, **arguments)
        if action == "apply":
            applied[0] += 1
            if applied[0] == 1:
                # It stalled past its lease, and another worker took the session: the lease is that worker's now.
                async with api.app.state.session_factory() as db:
                    await db.execute(text(
                        "UPDATE session_leases SET lease_token = gen_random_uuid(), owner_id = 'worker-two' WHERE session_id = :id"
                    ), {"id": thread.id})
                    await db.commit()
        return answer

    with monkeypatch.context() as first_run:
        first_run.setattr(Steps, "ask", asking)
        with pytest.raises(asyncio.CancelledError):
            await woken(api, monkeypatch, thread, [EDITS, _final_response("Done.")])
    # It wrote nothing more: the row is as its last write left it, nothing was put back, and nobody was told anything.
    [row] = await records(api, thread)
    assert row.saga_state == "running" and asked_by(here, thread, "land:0") == ["changed", "revisions", "pickup", "commit", "apply"]
    assert [(s["tool_name"], s["state"]) for s in row.steps if s["tool_name"] == "history.apply"] == [("history.apply", "pending")] * 2
    assert (here.folder / "Budget.xlsx").read_text() == "Total,42\n" and await left_open(api, thread) == []
    assert await told(api, master) == []
    assert await events_of(api, thread.id, EventType.INBOX_ACTION_REQUIRED, EventType.SESSION_COMPLETE) == []
    # The worker that holds the lease goes on from the journal: the second file, the record, the forgetting.
    async with api.app.state.session_factory() as db:
        await db.execute(text("DELETE FROM session_leases WHERE session_id = :id"), {"id": thread.id})
        await db.commit()
    await woken(api, monkeypatch, thread, [_final_response("Done.")])
    assert asked_by(here, thread, "land:0") == LANDED
    [row] = await records(api, thread)
    assert (row.saga_state, kept(here)) == ("completed", {})
    [report] = await told(api, master)
    assert files_of(report) == [("Budget.xlsx", "landed", None), ("Report.docx", "landed", None)] and "landing" not in report.data


async def test_a_landings_row_is_written_only_while_its_writer_holds_its_threads_lease(api):
    from surogates.devices.operations import LeaseLost

    device = await register(api)
    project, _, thread = await made_with_copy(api, device["id"])
    store, session_factory = api.app.state.session_store, api.app.state.session_factory
    lease = await store.try_acquire_lease(thread.id, "worker-one", ttl_seconds=60)
    saga = Saga(saga_id="saga:fenced", session_id=thread.id, kind="landing")
    row_id = await start_landing(
        session_factory, saga, workstream_id=project["id"], thread_id=thread.id, agent_id=str(thread.agent_id),
        user_id=thread.user_id, tool_saga_id=None, events=None, device_id=UUID(device["id"]), folder=FOLDER,
        held=(thread.id, str(lease.lease_token)),
    )
    row = landing_module._Row(session_factory, row_id, saga, held=(thread.id, str(lease.lease_token)))
    saga.steps.append(SagaStep(step_id="step:record", tool_name="history.record", tool_call_id="", arguments={"main": None}))
    await row.write()
    # Another worker takes the session over; this one's steps are an older run's, without the record step.
    await store.release_lease(thread.id, lease.lease_token)
    taken = await store.try_acquire_lease(thread.id, "worker-two", ttl_seconds=60)
    saga.steps.clear()
    for late in (row.write, row.alive, row.drop, lambda: landing_module._written(row.write, tries=2, state="escalated")):
        with pytest.raises(LeaseLost):
            await late()
    # Nor does it begin a row.
    assert await start_landing(
        session_factory, Saga(saga_id="saga:fenced-2", session_id=thread.id, kind="landing"), workstream_id=project["id"],
        thread_id=thread.id, agent_id=str(thread.agent_id), user_id=thread.user_id, tool_saga_id=None, events=None,
        device_id=UUID(device["id"]), folder=FOLDER, held=(thread.id, str(lease.lease_token)),
    ) is None
    async with session_factory() as db:
        rows = (await db.execute(select(WorkstreamHistory).where(WorkstreamHistory.thread_id == thread.id))).scalars().all()
    assert [(r.saga_id, r.saga_state, [s["tool_name"] for s in r.steps]) for r in rows] == [("saga:fenced", "running", ["history.record"])]
    # The worker that holds it writes as before.
    await landing_module._Row(session_factory, row_id, saga, held=(thread.id, str(taken.lease_token))).write(state="compensated")
    async with session_factory() as db:
        assert (await db.execute(select(WorkstreamHistory.saga_state).where(WorkstreamHistory.id == row_id))).scalar_one() == "compensated"
