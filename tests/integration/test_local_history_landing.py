"""A turn of a project's thread bound with a copy of its own lands in its folder on the user's computer, as one saga of device operations.

Each scene runs a worker's real wake on the tests' computer, and the turn's
end lands what it did in the thread's copy: the folder held and put right,
the copy's files looked at in the folder, your edits picked up, the turn
committed, each file applied by the land kind, the landing recorded and its
kept files forgotten.  The scenes that write the folder run twice: on the
tests' computer's land rules (``tests.fake_places``), and on the app's own
file helper in their place (marked ``desktop``).  The folder is read back by
name, mode, time and bytes.
"""

from __future__ import annotations

import json
import os
import stat
import time
from uuid import UUID, uuid5

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from surogates.db.models import WorkstreamHistory
from surogates.harness import landing as landing_module
from surogates.harness import local_landing
from surogates.harness.local_landing import land_local_turn
from surogates.harness.loop_context_replay import worker_note
from surogates.session.events import EventType
from tests.test_fake_places import Real, built_helper  # noqa: F401  (built_helper is a fixture)
from tests.test_steer_loop import _final_response

from .test_devices import FOLDER, api, link_url  # noqa: F401  (api and link_url are fixtures)
from .test_local_history_open import begun_with_copy, woken
from .test_local_history_threads import blob, computer, picture, tool  # noqa: F401  (computer is a fixture)
from .test_turn_sagas import calling
from .test_workstream_threads import events_of

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session", params=["its rules", pytest.param("the app's helper", marks=pytest.mark.desktop)])
async def here(request, computer):
    """The tests' computer, its land kind answered by its own rules, or by the app's file helper in their place."""
    started: list = []
    if request.param == "the app's helper":
        request.getfixturevalue("built_helper")

        def helper(*where):
            started.append(where)
            return Real(*where)

        computer.app.places.land_helper = helper
    yield computer
    computer.app.places.let_go()
    # Each scene here holds its folder: the app's helper answered its land kind, and the rules did not.
    assert started or request.param != "the app's helper"


def ran(here, invocation: str | None = None) -> list[tuple[str, str]]:
    """Each of a thread's own kinds *here*'s app ran, as (invocation, action), in order; under *invocation* alone, its actions."""
    asked = [(_named(called), action) for called, _, _, action in here.app.places.asked]
    return [action for called, action in asked if called == invocation] if invocation is not None else asked


def _named(invocation: str) -> str:
    """An invocation as a scene names it: a turn's release by what it is, its own name being new each time."""
    return invocation.rsplit(":", 1)[0] if ":release:" in invocation else invocation


def watched(monkeypatch, here) -> list[tuple]:
    """What the landing did, in order: each operation as its computer ran it, and each write of its row, with its steps."""
    log: list[tuple] = []
    run = here.app.places.run

    def running(frame):
        log.append(("op", _named(frame["invocation_id"]), frame["args"].get("action")))
        return run(frame)

    save = landing_module.save_landing

    async def saving(session_factory, row, saga, **values):
        log.append(("row", values.get("state", "running"), [(s.tool_name, s.state.value) for s in saga.steps]))
        return await save(session_factory, row, saga, **values)

    monkeypatch.setattr(here.app.places, "run", running)
    monkeypatch.setattr(landing_module, "save_landing", saving)
    return log


def holding(here, held) -> list[dict]:
    """*here*'s app takes each operation *held* names by its frame and answers none of them: the frames it holds."""
    frames: list[dict] = []
    handle = here.app._handle

    async def answering(frame, ws) -> None:
        if held(frame):
            frames.append(frame)
            return
        await handle(frame, ws)

    here.app._handle = answering
    return frames


def seen(folder) -> dict[str, tuple]:
    """Each entry of *folder*, by name: its kind, mode, file number, size, time and, for a file, its bytes' hash.

    Not when its entry last changed, which any put-back changes: the file put back is the one that was there.
    """
    return {path: (*entry[:5], entry[6]) for path, entry in picture(folder).items()}


def kept(here) -> dict[str, tuple[int, bytes]]:
    """What the folder's landings keep of the files they replaced: each kept file's mode and bytes, by its saga and step."""
    store = here.app.places.kept
    return {
        str(path.relative_to(store)): (stat.S_IMODE(path.stat().st_mode), path.read_bytes())
        for path in sorted(store.rglob("*")) if path.is_file() and path.suffix != ".json"
    } if store.is_dir() else {}


async def records(api, thread) -> list[WorkstreamHistory]:
    """The landings recorded for *thread*, oldest first."""
    async with api.app.state.session_factory() as db:
        return list((await db.execute(
            select(WorkstreamHistory).where(WorkstreamHistory.thread_id == thread.id).order_by(WorkstreamHistory.id)
        )).scalars())


def files_of(report) -> list[tuple]:
    return [(f["ref"], f["landing"], f.get("reason")) for f in report.data["files"] if f["kind"] == "file"]


async def told(api, master) -> list:
    """What the thread told its master, its reports oldest first."""
    return await events_of(api, master.id, EventType.WORKER_COMPLETE)


def said(report) -> str:
    return worker_note(report.type, report.data)["content"]


def lie_at(here, action: str, then, *, invocation: str = "land:0") -> None:
    """*here*'s app runs *action* under *invocation* as asked, then *then* happens (with the frame and its outcome), which may answer in its place."""
    def lie(frame, outcome):
        if (frame["invocation_id"], frame["args"].get("action")) == (invocation, action):
            return then(frame, outcome) or outcome
        return outcome

    here.app.lie = lie


EDITS = calling(("write_file", {"path": "Budget.xlsx", "content": "Total,42\n"}), ("terminal", {"command": "printf ' edited' >> Report.docx"}))
LANDED = ["changed", "revisions", "pickup", "commit", "apply", "apply", "record", "forget"]


# -- a turn's end lands it -----------------------------------------------------------------------------------------


async def test_a_turn_lands_in_its_folder_as_one_saga_in_its_computers_order_its_row_written_before_each_step_it_needs(api, here, monkeypatch):
    project, master, thread = await begun_with_copy(api, here)
    folder, before = here.folder, seen(here.folder)
    log = watched(monkeypatch, here)
    began = time.time_ns()
    await woken(api, monkeypatch, thread, [EDITS, _final_response("The totals are in Budget.xlsx.")])
    # Its two files are in the folder, your report still yours to read alone; every other entry is as it was.
    assert (folder / "Budget.xlsx").read_text() == "Total,42\n" and (folder / "Report.docx").read_bytes() == b"PK report v1 edited"
    report = (folder / "Report.docx").stat()
    assert stat.S_IMODE(report.st_mode) == 0o600 and report.st_mtime_ns >= began
    after = seen(folder)
    assert {p: e for p, e in after.items() if p != "Budget.xlsx"} | {"Report.docx": None} == before | {"Report.docx": None}
    # One saga, its steps device operations under the turn's own name, in the computer's order: the folder held and
    # put right first; the copy's files looked at in the folder before your edits are picked up and the turn committed;
    # each file by the land kind; the record; and the forgetting of what the landing kept, which lets the folder go.
    assert [entry[1:] for entry in log if entry[0] == "op"] == [
        ("open:0", "open"), ("land:0:hold", "recover"), *(("land:0", action) for action in LANDED),
    ]
    # Its row held every apply, none done, before the first was sent; and its record before the record was sent.
    first = log.index(("op", "land:0", "apply"))
    fixed = [steps for kind, _, steps in log[:first] if kind == "row" and ("history.apply", "pending") in steps]
    assert fixed and {state for name, state in fixed[-1] if name == "history.apply"} == {"pending"}
    record = log.index(("op", "land:0", "record"))
    assert any(kind == "row" and ("history.record", "pending") in steps for kind, _, steps in log[first:record])
    # Completed in its row before it was forgotten, and the forgetting is a step of its row.
    completed = next(n for n, entry in enumerate(log) if entry[:2] == ("row", "completed"))
    assert record < completed < log.index(("op", "land:0", "forget"))
    assert here.app.places.holder is None and kept(here) == {}
    [row] = await records(api, thread)
    assert (row.saga_id, row.saga_state, row.kind) == (f"saga:{uuid5(thread.id, 'land:0')}", "completed", "landing")
    assert [(s["tool_name"], s["state"]) for s in row.steps] == [
        ("history.pickup", "committed"), ("history.commit", "committed"), ("history.apply", "committed"),
        ("history.apply", "committed"), ("history.record", "committed"), ("history.forget", "committed"),
    ]
    # The record is of that folder on that computer, as the server stamped them on the thread: no answer names either.
    assert (str(row.device_id), row.folder, str(row.workstream_id)) == (here.device_id, FOLDER, thread.config["workstream_id"])
    assert sorted((f["path"], f["merged"], f["after"]) for f in row.files) == [
        ("Budget.xlsx", True, blob(b"Total,42\n")), ("Report.docx", True, blob(b"PK report v1 edited")),
    ]
    # What is recorded of your files is their names and git's ids of their versions, never their bytes.
    assert "Total,42" not in json.dumps([row.steps, row.files, row.picked_up])
    [report] = await told(api, master)
    assert files_of(report) == [("Budget.xlsx", "landed", None), ("Report.docx", "landed", None)] and "landing" not in report.data
    [end] = await events_of(api, thread.id, EventType.SESSION_COMPLETE)
    assert end.data["saved"] is True
    # A file's History lists that folder's versions, none of which can be had from here.
    listed = await api.client.get(
        f"/v1/workstreams/{project['id']}/history", params={"path": "Report.docx", "device_id": here.device_id}, headers=api.auth(),
    )
    assert listed.status_code == 200, listed.text
    assert [(v["id"], v["change"], v["available"]) for v in listed.json()] == [(f"{row.id}:f", "changed", False), (f"{row.id}:b", "added", False)]


async def test_a_turn_that_only_talks_asks_its_computer_nothing_at_its_end(api, here, monkeypatch):
    _, master, thread = await begun_with_copy(api, here)
    await woken(api, monkeypatch, thread, [EDITS, _final_response("Done.")])
    landed = len(here.app.places.asked)
    ended = (await events_of(api, thread.id, EventType.SESSION_COMPLETE))[-1].id
    await woken(api, monkeypatch, thread, [calling(("read_file", {"path": "Report.docx"})), _final_response("Read it.")], said="Read it.")
    # Its turn's open, and nothing of a landing.
    assert ran(here)[landed:] == [(f"open:{ended}", "open")]
    assert len(await records(api, thread)) == 1 and len(await told(api, master)) == 2


# -- a save of yours while it lands --------------------------------------------------------------------------------


async def test_a_file_you_save_after_the_landings_look_is_picked_up_as_yours_and_never_written_over(api, here, monkeypatch):
    _, master, thread = await begun_with_copy(api, here)
    report = here.folder / "Report.docx"
    yours: dict = {}

    def you_save(frame, outcome) -> None:
        report.write_bytes(b"PK report v2, by you")
        yours.update(seen(here.folder))

    lie_at(here, "revisions", you_save)
    await woken(api, monkeypatch, thread, [EDITS, _final_response("Edited the report.")])
    # Your save stands as you made it, entry for entry; the thread's other file landed; nothing was put back.
    assert seen(here.folder)["Report.docx"] == yours["Report.docx"] and report.read_bytes() == b"PK report v2, by you"
    assert (here.folder / "Budget.xlsx").read_text() == "Total,42\n"
    assert ran(here, "land:0") == ["changed", "revisions", "pickup", "commit", "apply", "record", "forget"]
    # Your save is a version in the folder's history, by you; the thread's change to it waits, and is redone on it.
    [row] = await records(api, thread)
    assert [(p["path"], p["before"], p["after"]) for p in row.picked_up] == [
        ("Report.docx", blob(b"PK report v1"), blob(b"PK report v2, by you")),
    ]
    assert sorted((f["path"], f["merged"]) for f in row.files) == [("Budget.xlsx", True), ("Report.docx", False)]
    [redo] = await events_of(api, thread.id, EventType.HISTORY_REDO)
    assert redo.data["files"] == [{"path": "Report.docx", "reason": "changed", "by": {"kind": "you"}}]
    [report_] = await told(api, master)
    assert files_of(report_) == [("Budget.xlsx", "landed", None), ("Report.docx", "redoing", "changed")]
    assert kept(here) == {} and here.app.places.holder is None


async def test_a_file_you_save_after_the_turn_was_committed_is_refused_at_its_apply_and_the_landing_lands_again_around_it(api, here, monkeypatch):
    _, master, thread = await begun_with_copy(api, here)
    report = here.folder / "Report.docx"
    yours: dict = {}

    def you_save(frame, outcome) -> None:
        report.write_bytes(b"PK report v2, saved while it landed")
        yours.update(seen(here.folder))

    lie_at(here, "commit", you_save)
    await woken(api, monkeypatch, thread, [EDITS, _final_response("Edited the report.")])
    # Your save stands as you made it, entry for entry: the apply found it, and wrote nothing over it.
    assert seen(here.folder)["Report.docx"] == yours["Report.docx"]
    assert (here.folder / "Budget.xlsx").read_text() == "Total,42\n"
    # The first landing wrote the budget, was refused the report, and took the budget back, newest first, then forgot.
    assert ran(here, "land:0") == ["changed", "revisions", "pickup", "commit", "apply", "apply", "unapply", "unapply", "forget"]
    # Tried once more, at once, as a new saga: its pickup sees your save, and the report is an ordinary overlap.
    assert ran(here, "land:0:2") == ["changed", "revisions", "pickup", "commit", "apply", "record", "forget"]
    put_back, landed = await records(api, thread)
    assert (put_back.saga_state, put_back.commit, landed.saga_state) == ("compensated", None, "completed")
    assert landed.saga_id == f"saga:{uuid5(thread.id, 'land:0:2')}"
    # Put back whole, it was forgotten: the apply your save refused wrote nothing, and is not asked to be what it was.
    forgetting = put_back.steps[-1]
    assert (forgetting["tool_name"], forgetting["state"]) == ("history.forget", "committed")
    assert [f["path"] for f in forgetting["arguments"]["applied"]] == ["Budget.xlsx"]
    [report_] = await told(api, master)
    assert files_of(report_) == [("Budget.xlsx", "landed", None), ("Report.docx", "redoing", "changed")]
    assert kept(here) == {} and here.app.places.holder is None


async def test_a_file_already_as_the_turn_left_it_is_written_by_no_apply_and_each_apply_keeps_its_files_place_in_the_commit(api, here, monkeypatch):
    _, master, thread = await begun_with_copy(api, here)
    yours: dict = {}

    async def you_do_the_same(harness) -> None:
        # While the thread works, you make the file it makes, as it makes it, and delete the file it deletes.
        (here.folder / "A.md").write_text("same\n")
        (here.folder / "Plans" / "Q3.md").unlink()
        yours.update(seen(here.folder))

    await woken(api, monkeypatch, thread, [
        calling(("terminal", {"command": "echo same > A.md && rm Plans/Q3.md && echo Total,42 > Budget.xlsx"})),
        calling(("memory", {"action": "add", "content": "Done."})),
        _final_response("Done."),
    ], during=you_do_the_same)
    # One apply, the budget's, numbered by its place among the commit's changes: the other two need none.
    assert ran(here, "land:0") == ["changed", "revisions", "pickup", "commit", "apply", "record", "forget"]
    [row] = await records(api, thread)
    commit = next(s for s in row.steps if s["tool_name"] == "history.commit")
    assert [(c["path"], c["before"] == c["after"]) for c in commit["result"]["changes"]] == [
        ("A.md", True), ("Budget.xlsx", False), ("Plans/Q3.md", True),
    ]
    assert [s["arguments"]["step"] for s in row.steps if s["tool_name"] == "history.apply"] == [1]
    # Your file stands as you made it; the turn's deletion is yours too; each is recorded as the landing's.
    assert seen(here.folder)["A.md"] == yours["A.md"] and not (here.folder / "Plans" / "Q3.md").exists()
    assert (here.folder / "Budget.xlsx").read_text() == "Total,42\n"
    assert sorted((f["path"], f["merged"], f["after"] is None) for f in row.files) == [
        ("A.md", True, False), ("Budget.xlsx", True, False), ("Plans/Q3.md", True, True),
    ]
    assert kept(here) == {} and here.app.places.holder is None


# -- its record ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("refusal", [
    {"type": "interrupted", "message": "This computer stopped while it ran"},
    {"type": "history", "code": "failed", "message": "git said nothing"},
    {"type": "cancelled", "message": "Cancelled"},
], ids=["interrupted", "the history's failure", "cancelled"])
async def test_a_record_refused_after_its_push_completes_the_landing_and_puts_nothing_back(api, here, monkeypatch, refusal):
    _, master, thread = await begun_with_copy(api, here)
    lie_at(here, "record", lambda frame, outcome: {"error": refusal})
    await woken(api, monkeypatch, thread, [EDITS, _final_response("Done.")])
    # The history says whether it pushed before anything goes back: it did, so nothing goes back.
    assert ran(here, "land:0") == ["changed", "revisions", "pickup", "commit", "apply", "apply", "record", "fetch", "forget"]
    assert (here.folder / "Budget.xlsx").read_text() == "Total,42\n" and (here.folder / "Report.docx").read_bytes() == b"PK report v1 edited"
    [row] = await records(api, thread)
    assert row.saga_state == "completed" and row.commit is not None
    assert sorted((f["path"], f["merged"]) for f in row.files) == [("Budget.xlsx", True), ("Report.docx", True)]
    [report] = await told(api, master)
    assert files_of(report) == [("Budget.xlsx", "landed", None), ("Report.docx", "landed", None)] and "landing" not in report.data
    assert kept(here) == {} and here.app.places.holder is None


async def test_a_record_its_computer_never_answers_leaves_the_landing_unsettled_with_nothing_put_back_and_nothing_forgotten(api, here, monkeypatch):
    monkeypatch.setattr(local_landing, "STEP_WAIT", 2)
    _, master, thread = await begun_with_copy(api, here)
    frames = holding(here, lambda frame: frame["kind"] == "history" and frame["args"].get("action") == "record")
    await woken(api, monkeypatch, thread, [EDITS, _final_response("Done.")])
    # The record went, and nothing after it: no look at the history, no put-back, no forgetting.
    assert [frame["invocation_id"] for frame in frames] == ["land:0"]
    assert ran(here, "land:0") == ["changed", "revisions", "pickup", "commit", "apply", "apply"]
    # The folder holds what was applied, and what it replaced is kept: whoever settles the landing has all it needs.
    assert (here.folder / "Budget.xlsx").read_text() == "Total,42\n" and (here.folder / "Report.docx").read_bytes() == b"PK report v1 edited"
    saga = f"saga:{uuid5(thread.id, 'land:0')}"
    assert kept(here) == {f"{saga}/1": (0o600, b"PK report v1")} and here.app.places.holder == str(thread.id)
    [row] = await records(api, thread)
    assert row.saga_state == "running"
    assert [(s["tool_name"], s["state"]) for s in row.steps] == [
        ("history.pickup", "committed"), ("history.commit", "committed"), ("history.apply", "committed"),
        ("history.apply", "committed"), ("history.record", "failed"),
    ]
    [report] = await told(api, master)
    assert report.data["landing"] == "unsettled" and "Not finished landing" in said(report)
    [end] = await events_of(api, thread.id, EventType.SESSION_COMPLETE)
    assert end.data["saved"] is False


# -- its row -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("where", ["before the first apply", "before the record"])
async def test_a_landing_whose_row_cannot_be_written_where_it_must_be_writes_no_file_and_records_nothing(api, here, monkeypatch, where):
    _, master, thread = await begun_with_copy(api, here)
    folder = seen(here.folder)
    save = landing_module.save_landing

    async def refused(session_factory, row, saga, **values):
        named = {s.tool_name: s.state.value for s in saga.steps}
        at = named.get("history.apply") == "pending" if where == "before the first apply" else named.get("history.record") == "pending"
        if values.get("state", "running") == "running" and at:
            raise ConnectionError("the database went away")
        return await save(session_factory, row, saga, **values)

    monkeypatch.setattr(landing_module, "save_landing", refused)
    await woken(api, monkeypatch, thread, [EDITS, _final_response("Done.")])
    # No file of the turn's reached the folder, or stayed there: entry for entry, it is as it was.
    assert seen(here.folder) == folder and kept(here) == {} and here.app.places.holder is None
    if where == "before the first apply":
        assert ran(here, "land:0") == ran(here, "land:0:2") == ["changed", "revisions", "pickup", "commit", "forget"]
    else:
        # Applied, never recorded: the history is asked first, then each is put back, newest first.
        assert ran(here, "land:0") == ran(here, "land:0:2") == [
            "changed", "revisions", "pickup", "commit", "apply", "apply", "fetch", "unapply", "unapply", "forget",
        ]
    assert [row.saga_state for row in await records(api, thread)] == ["compensated", "compensated"]
    [report] = await told(api, master)
    assert (report.data["landing"], report.data["landing_reason"]) == ("compensated", "unwritten")
    assert "its record on the server could not be written" in said(report)
    [end] = await events_of(api, thread.id, EventType.SESSION_COMPLETE)
    assert end.data["saved"] is False


async def test_an_apply_cancelled_after_it_wrote_is_put_back_and_the_landing_is_not_tried_again(api, here, monkeypatch):
    _, master, thread = await begun_with_copy(api, here)
    folder = seen(here.folder)
    # As a Stop's pause closes an operation its computer had already run: the file was written, and its answer is lost.
    lie_at(here, "apply", lambda frame, outcome: {"error": {"type": "cancelled", "message": "Cancelled"}} if frame["args"]["step"] == 0 else None)
    await woken(api, monkeypatch, thread, [EDITS, _final_response("Done.")])
    assert ran(here, "land:0") == ["changed", "revisions", "pickup", "commit", "apply", "unapply", "forget"] and ran(here, "land:0:2") == []
    assert seen(here.folder) == folder and kept(here) == {} and here.app.places.holder is None
    [row] = await records(api, thread)
    assert row.saga_state == "compensated"
    [report] = await told(api, master)
    assert (report.data["landing"], report.data["landing_reason"], report.data["landing_code"]) == ("compensated", "refused", "cancelled")


# -- a landing that cannot be put back whole ------------------------------------------------------------------------


async def test_a_landing_that_cannot_be_put_back_whole_keeps_what_it_kept_gives_the_folder_back_and_says_where_its_files_are(api, here, monkeypatch):
    _, master, thread = await begun_with_copy(api, here)
    report, zeta = here.folder / "Report.docx", here.folder / "Zeta.md"
    yours: dict = {}

    def you_make_zeta(frame, outcome) -> None:
        # After the turn was committed: the landing's apply finds it, and writes nothing over it.
        zeta.write_text("Zeta, by you\n")

    def you_save_the_report(frame, outcome) -> None:
        # After the landing wrote it: its put-back finds yours, and leaves it.
        if frame["args"]["path"] == "Report.docx":
            report.write_bytes(b"PK report v3, by you")
            yours.update(seen(here.folder))

    def both(frame, outcome):
        action = frame["args"].get("action")
        if frame["invocation_id"] == "land:0" and action in ("commit", "apply"):
            (you_make_zeta if action == "commit" else you_save_the_report)(frame, outcome)
        return outcome

    here.app.lie = both
    await woken(api, monkeypatch, thread, [
        calling(("terminal", {"command": "printf ' edited' >> Report.docx && echo zeta > Zeta.md"})), _final_response("Done."),
    ])
    # Your two files stand as you left them, entry for entry.
    assert {p: seen(here.folder)[p] for p in ("Report.docx", "Zeta.md")} == {p: yours[p] for p in ("Report.docx", "Zeta.md")}
    # What the landing replaced is still kept, with your mode, as you had it before it: no forgetting was asked.
    saga = f"saga:{uuid5(thread.id, 'land:0')}"
    assert kept(here) == {f"{saga}/0": (0o600, b"PK report v1")}
    assert ran(here, "land:0") == ["changed", "revisions", "pickup", "commit", "apply", "apply", "unapply", "unapply"]
    # Not tried again; and the folder given back by the turn's own hold, which drops nothing a landing kept.
    assert ran(here, "land:0:2") == [] and ran(here, "land:0:release") == ["forget"] and here.app.places.holder is None
    [row] = await records(api, thread)
    assert row.saga_state == "escalated"
    [waits] = await events_of(api, thread.id, EventType.INBOX_ACTION_REQUIRED)
    words = waits.data["instructions"]
    assert "Report.docx, Zeta.md" in words and FOLDER in words and "Flavius's ThinkPad" in words and "History" not in words
    assert (waits.data["title"], waits.data["target"], waits.data["escalated"]) == ("Couldn't finish landing my changes", "session", True)
    [told_] = await told(api, master)
    assert told_.data["landing"] == "escalated"


# -- what it leaves out --------------------------------------------------------------------------------------------


async def test_a_landing_writes_no_name_that_runs_code_and_replaces_no_file_with_a_second_name(api, here, monkeypatch):
    (here.folder / "Twin.txt").write_text("one\n")
    os.link(here.folder / "Twin.txt", here.folder.parent / "its-other-name.txt")
    _, master, thread = await begun_with_copy(api, here)
    folder = seen(here.folder)
    await woken(api, monkeypatch, thread, [
        calling(("terminal", {"command": "mkdir .vscode && echo '{}' > .vscode/tasks.json && echo two >> Twin.txt && echo Total,42 > Budget.xlsx"})),
        _final_response("Done."),
    ])
    assert {p: e for p, e in seen(here.folder).items() if p != "Budget.xlsx"} == folder
    assert (here.folder / "Budget.xlsx").read_text() == "Total,42\n"
    [report] = await told(api, master)
    assert files_of(report) == [
        (".vscode/tasks.json", "not_merged", "protected"), ("Budget.xlsx", "landed", None), ("Twin.txt", "not_merged", "linked"),
    ]
    # Nothing waits on you, and nothing is redone: no one else changed them.
    assert await events_of(api, thread.id, EventType.HISTORY_REDO, EventType.INBOX_ACTION_REQUIRED) == []
    words = said(report)
    assert "could run code on the user's computer" in words and "a file with a second name" in words
    # Each stays in the thread's own copy as the thread left it: the landing wrote neither, and took neither away.
    copy = here.app.places.copy(str(thread.id))
    assert (copy / ".vscode" / "tasks.json").read_text() == "{}\n" and (copy / "Twin.txt").read_text() == "one\ntwo\n"
    # Nor is it work the turn's end failed to save: a turn that only talks lands nothing for it.
    [end] = await events_of(api, thread.id, EventType.SESSION_COMPLETE)
    assert end.data["saved"] is True
    before = len(here.app.places.asked)
    await woken(api, monkeypatch, thread, [_final_response("Nothing more.")], said="Anything else?")
    assert [action for _, action in ran(here)[before:]] == ["open"]


async def test_a_copy_still_written_while_its_landing_runs_is_put_back_once_and_lands_with_the_next_turn(api, here, monkeypatch):
    _, master, thread = await begun_with_copy(api, here)
    copy = here.app.places.copy(str(thread.id))
    folder = seen(here.folder)

    def still_writing(frame, outcome) -> None:
        # A command still running, or a helper: it writes a file of the landing after the turn was committed.
        (copy / "Notes.md").write_text("notes, written again\n")

    lie_at(here, "commit", still_writing)
    await woken(api, monkeypatch, thread, [
        calling(("terminal", {"command": "echo Total,42 > Budget.xlsx && echo notes > Notes.md"})), _final_response("Done."),
    ])
    # Put back whole, once: a second try at once would meet the same writer.
    assert ran(here, "land:0") == ["changed", "revisions", "pickup", "commit", "apply", "apply", "unapply", "unapply", "forget"]
    assert ran(here, "land:0:2") == [] and seen(here.folder) == folder and kept(here) == {}
    [row] = await records(api, thread)
    assert (row.saga_state, row.commit) == ("compensated", None)
    [report] = await told(api, master)
    assert (report.data["landing"], report.data["landing_reason"]) == ("compensated", "stale")
    assert "was still being written while its work landed" in said(report) and "in its copy on its computer" in said(report)
    # Nothing waits on you: its work is in its copy, and lands with its next turn, though that turn takes no step.
    assert await events_of(api, thread.id, EventType.INBOX_ACTION_REQUIRED) == []
    here.app.lie = None
    await woken(api, monkeypatch, thread, [_final_response("Done.")], said="Done?")
    assert (here.folder / "Notes.md").read_text() == "notes, written again\n" and (here.folder / "Budget.xlsx").read_text() == "Total,42\n"


async def test_a_landings_row_holds_the_files_it_asked_to_apply_never_what_its_computer_says_it_applied(api, here, monkeypatch):
    _, master, thread = await begun_with_copy(api, here)
    folder = seen(here.folder)
    here.app.lie = lambda frame, outcome: (
        {"ok": {"path": "Payroll.xlsx", "before": None, "after": "c" * 40, "made": []}}
        if frame["kind"] == "land" and frame["args"].get("action") == "apply" else outcome
    )
    await woken(api, monkeypatch, thread, [
        calling(("write_file", {"path": "Budget.xlsx", "content": "Total,42\n"})), _final_response("Done."),
    ])
    # An answer that names another file than the step asked for is no answer to it: the landing is put back, twice.
    rows = await records(api, thread)
    assert [row.saga_state for row in rows] == ["compensated", "compensated"]
    assert "Payroll.xlsx" not in json.dumps([[row.steps, row.files] for row in rows])
    [report] = await told(api, master)
    assert files_of(report) == [("Budget.xlsx", "not_merged", None)] and seen(here.folder) == folder and kept(here) == {}


# -- what one landing carries --------------------------------------------------------------------------------------


async def test_a_turn_with_more_changes_than_one_landing_carries_asks_no_commit_and_says_its_work_stays_in_its_copy(api, here, monkeypatch):
    monkeypatch.setattr(local_landing, "CARRIES", 300)
    _, master, thread = await begun_with_copy(api, here)
    folder = seen(here.folder)
    await woken(api, monkeypatch, thread, [EDITS, _final_response("Done.")])
    # Named before anything is committed: neither your edits nor the turn are, and the folder is let go.
    assert ran(here, "land:0") == ["changed", "forget"] and ran(here, "land:0:2") == []
    assert seen(here.folder) == folder and here.app.places.holder is None
    [report] = await told(api, master)
    assert (report.data["landing"], report.data["landing_reason"]) == ("compensated", "too_large")
    assert "lands once fewer of its files are changed" in said(report)


async def test_a_commit_whose_answer_does_not_fit_the_link_is_asked_once_and_the_turn_says_it_could_not_land(api, here, monkeypatch):
    _, master, thread = await begun_with_copy(api, here)
    folder = seen(here.folder)
    # As the app answers an operation that ran and whose result does not fit one frame of the link.
    lie_at(here, "commit", lambda frame, outcome: {"error": {
        "type": "too_large", "message": "The operation ran, but its result is too large to send. Check what it did before repeating it.",
    }})
    await woken(api, monkeypatch, thread, [EDITS, _final_response("Done.")])
    assert ran(here, "land:0") == ["changed", "revisions", "pickup", "commit", "forget"] and ran(here, "land:0:2") == []
    assert seen(here.folder) == folder
    [report] = await told(api, master)
    assert (report.data["landing"], report.data["landing_reason"]) == ("compensated", "too_large")


# -- the folder held -----------------------------------------------------------------------------------------------


async def test_what_the_folders_helper_found_left_by_a_landing_cut_short_is_told_to_the_master(api, computer, monkeypatch):
    _, master, thread = await begun_with_copy(api, computer)
    found = {"restored": ["Plans/Q3.md"], "beside": [["Report.docx", "Report (kept by Surogate).docx"]], "lost": [], "unread": [["saga:old", 2, None]]}
    lie_at(computer, "recover", lambda frame, outcome: {"ok": found}, invocation="land:0:hold")
    await woken(api, monkeypatch, thread, [EDITS, _final_response("Done.")])
    [report] = await told(api, master)
    assert report.data["recovery"] == {"beside": found["beside"], "unread": found["unread"]}
    words = said(report)
    assert "Kept beside a newer file of its name, after a landing in its folder was cut short: Report.docx" in words
    assert "as its record there could not be read: a file" in words
    # And the turn landed all the same.
    assert files_of(report) == [("Budget.xlsx", "landed", None), ("Report.docx", "landed", None)]


async def test_a_turns_end_taken_up_again_finds_its_landing_ended_and_gives_its_hold_of_the_folder_back(api, computer, monkeypatch):
    _, master, thread = await begun_with_copy(api, computer)
    store = api.app.state.session_store
    assert not (await tool(api, thread, "terminal", command="printf ' edited' >> Report.docx")).get("error")
    lease = await store.try_acquire_lease(thread.id, "worker-local", ttl_seconds=60)

    async def lands() -> dict:
        return await land_local_turn(
            store=store, session_factory=api.app.state.session_factory, redis=api.app.state.redis,
            session=await store.get_session(thread.id), lease_token=str(lease.lease_token), saga_settings=None, tool_saga_id=None,
        )

    class Lost(BaseException):
        """A worker lost: nothing of its turn runs on, not even what cleans up after an error."""

    async def lost(*args, **kwargs):
        raise Lost

    with monkeypatch.context() as cut:
        cut.setattr(local_landing, "_forgotten", lost)
        with pytest.raises(Lost):
            await lands()
    # Recorded, and lost before its forgetting: the folder is held for the thread, and what it replaced is kept.
    saga = f"saga:{uuid5(thread.id, 'land:0')}"
    assert computer.app.places.holder == str(thread.id) and kept(computer) == {f"{saga}/0": (0o600, b"PK report v1")}
    before = len(computer.app.places.asked)
    again = await lands()
    # Taken up again, it finds its landing ended: its computer is asked only to give the folder back.
    assert ran(computer)[before:] == [("land:0:release", "forget")] and computer.app.places.holder is None
    assert (again["state"], [(f["ref"], f["landing"]) for f in again["files"]]) == ("completed", [("Report.docx", "landed")])
    # What it kept stays for the next landing in the folder to forget: the hold's forgetting drops none of it.
    assert kept(computer) == {f"{saga}/0": (0o600, b"PK report v1")}
    await store.release_lease(thread.id, lease.lease_token)


# -- whose rows a pod settles --------------------------------------------------------------------------------------


async def test_a_computers_landing_left_running_is_none_of_a_pods(api, here, monkeypatch):
    project, master, thread = await begun_with_copy(api, here)
    monkeypatch.setattr(local_landing, "STEP_WAIT", 2)
    holding(here, lambda frame: frame["kind"] == "history" and frame["args"].get("action") == "record")
    await woken(api, monkeypatch, thread, [EDITS, _final_response("Done.")])
    [row] = await records(api, thread)
    assert row.saga_state == "running"
    # The project's landings a pod settles are the cloud's alone.
    assert await landing_module.running_landings(api.app.state.session_factory, UUID(project["id"])) == []
    async with api.app.state.session_factory() as db:
        states = (await db.execute(text("SELECT saga_state FROM workstream_history WHERE id = :id"), {"id": row.id})).scalars().all()
    assert states == ["running"]
