"""A landing on the user's computer records your edits in its folder since ``main`` in parts, each of which one answer carries.

Your edits since the folder's last landing are any number of files, and one
answer of its computer's is one frame of the link: the pickup's own answer
names the first part of them, and each part after it is a step of the
landing's own, asked from where the last one ended.  So a folder where you
added thousands of files lands again, the History lists each as yours, and
a landing cut at any part of its pickup is finished by the next landing, with
your edits recorded once.  Each scene runs a worker's real wake on the tests'
computer, with real git; the folder is read back by name, mode, time and bytes.
"""

from __future__ import annotations

import shutil
from collections import Counter
from uuid import UUID

import pytest
from sqlalchemy import select

from surogates.db.models import WorkstreamHistory
from surogates.harness import landing as landing_module
from surogates.harness import local_landing as local_landing_module
from surogates.sandbox import local_history
from surogates.sandbox.history import HISTORY_CAP
from surogates.session.events import EventType
from tests.test_steer_loop import _final_response

from .test_devices import api, link_url  # noqa: F401  (api and link_url are fixtures)
from .test_local_history_landing import EDITS, files_of, kept, lie_at, ran, records, said, seen, told
from .test_local_history_open import begun_with_copy, woken
from .test_local_history_settle import (
    Lost,
    asked_by,
    killed_while_it_waits,
    lands_next,
    left_open,
    lost_after,
    settles,
    two_threads,
)
from .test_local_history_threads import blob, computer  # noqa: F401  (computer is a fixture)
from .test_turn_sagas import calling
from .test_workstream_threads import events_of

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: The thread's own turn: one file; and a step while which you add your photos to the folder, after its open.
BUDGET = calling(("write_file", {"path": "Budget.xlsx", "content": "Total,42\n"}))
ADD = calling(("memory", {"action": "add", "content": "Done."}))
#: A later turn's own file.
NOTES = calling(("write_file", {"path": "Notes.md", "content": "n\n"}))
#: What a part of a pickup's files holds, made small: twenty photos are three answers, nine, nine and two.
SMALL_PART = 1_000
#: Why a landing records none of your changes, and what you can do, as its report says it.
TOO_MANY = "the user added, changed, moved or deleted more files in its folder since the folder's last landing than one landing can record"
WHAT_TO_DO = "for now the user can move some of the files they added out of it, or start a thread on a folder inside it"
TWO_TURNS = "A folder they renamed or moved lands in two turns: moved out of the folder, then back in once a turn has ended"


def photos(many: int, under: str = "Photos/2026") -> list[str]:
    return [f"{under}/IMG_{n:06d}.jpg" for n in range(many)]


def you_add(folder, many: int, under: str = "Photos/2026") -> dict:
    """*many* photos you add to *folder*, named as a camera names them; the folder as you left it."""
    (folder / under).mkdir(parents=True, exist_ok=True)
    for n, path in enumerate(photos(many, under)):
        (folder / path).write_bytes(b"\xff\xd8 photo %d" % n)
    return seen(folder)


def adding(folder, many: int, yours: dict, under: str = "Photos/2026"):
    """While the thread works, you add *many* photos to *folder*: *yours* is the folder as you left it."""
    async def you_add_them(harness) -> None:
        yours.update(you_add(folder, many, under))

    return you_add_them


def too_many(report) -> None:
    """*report* says its landing recorded none of your changes, why, and what you can do."""
    assert (report.data["landing"], report.data["landing_reason"]) == ("compensated", "yours_too_large")
    words = said(report)
    assert TOO_MANY in words and WHAT_TO_DO in words and TWO_TURNS in words
    assert "of its files" not in words and "lands with its next turn" not in words


async def recorded_as_yours(api, workstream_id) -> Counter:
    """How many times the project's completed landings recorded each file as picked up, yours."""
    async with api.app.state.session_factory() as db:
        rows = (await db.execute(
            select(WorkstreamHistory).where(WorkstreamHistory.workstream_id == workstream_id, WorkstreamHistory.saga_state == "completed")
        )).scalars().all()
    return Counter(entry["path"] for row in rows for entry in row.picked_up)


async def history_of(api, project, here, path: str) -> list[tuple]:
    """*path*'s History in *project*, of its folder on *here*'s computer: each version's change and who made it."""
    listed = await api.client.get(
        f"/v1/workstreams/{project['id']}/history", params={"path": path, "device_id": here.device_id}, headers=api.auth(),
    )
    assert listed.status_code == 200, listed.text
    return [(v["change"], v["by"]) for v in listed.json()]


# -- a folder where you added thousands of files lands again --------------------------------------------------------


@pytest.mark.slow
async def test_a_turn_lands_in_a_folder_where_you_added_twenty_thousand_files_and_the_history_lists_each_as_yours(api, computer, monkeypatch):
    project, master, thread = await begun_with_copy(api, computer)
    yours: dict = {}
    await woken(api, monkeypatch, thread, [BUDGET, ADD, _final_response("The totals are in Budget.xlsx.")], during=adding(computer.folder, 20_000, yours))
    # The thread's file landed; your twenty thousand photos, and every other entry, are as you left them.
    assert (computer.folder / "Budget.xlsx").read_text() == "Total,42\n"
    after = seen(computer.folder)
    assert {path: entry for path, entry in after.items() if path != "Budget.xlsx"} == yours
    # Your edits came in three answers, the pickup's own and two parts, each a step of the landing's; then its turn.
    assert ran(computer, "land:0") == ["changed", "revisions", "pickup", "picked", "picked", "commit", "apply", "record", "forget"]
    [row] = await records(api, thread)
    assert row.saga_state == "completed"
    assert [s["tool_name"] for s in row.steps] == [
        "history.pickup", "history.picked", "history.picked", "history.commit", "history.apply", "history.record", "history.forget",
    ]
    # Recorded as yours, each once and as you saved it; the thread's file as its own.
    assert [(p["path"], p["before"], p["after"]) for p in row.picked_up] == [
        (path, None, blob(b"\xff\xd8 photo %d" % n)) for n, path in enumerate(photos(20_000))
    ]
    assert [(f["path"], f["merged"]) for f in row.files] == [("Budget.xlsx", True)]
    for path in ("Photos/2026/IMG_000000.jpg", "Photos/2026/IMG_012345.jpg", "Photos/2026/IMG_019999.jpg"):
        assert await history_of(api, project, computer, path) == [("added", {"kind": "you"})]
    assert [(change, by["kind"]) for change, by in await history_of(api, project, computer, "Budget.xlsx")] == [("added", "thread")]
    [report] = await told(api, master)
    assert files_of(report) == [("Budget.xlsx", "landed", None)] and "landing" not in report.data
    assert kept(computer) == {} and computer.app.places.holder is None
    # And the next landing there picks up nothing more: your photos are on main.
    await woken(api, monkeypatch, thread, [calling(("write_file", {"path": "Notes.md", "content": "n\n"})), _final_response("Done.")], said="More.")
    first, second = await records(api, thread)
    assert (second.saga_state, second.picked_up) == ("completed", [])


@pytest.mark.slow
async def test_more_of_your_changes_than_the_folders_history_keeps_are_refused_in_their_own_words_and_leave_nothing_of_the_landing(
    api, computer, monkeypatch,
):
    _, master, thread = await begun_with_copy(api, computer)
    yours: dict = {}
    # More than two of the history's whole folders differ by: no folder within its cap moves so much at once.
    many = 2 * HISTORY_CAP + 1
    await woken(api, monkeypatch, thread, [BUDGET, ADD, _final_response("Done.")], during=adding(computer.folder, many, yours))
    # The pickup's first answer says how many: more than one landing records, so nothing more is asked but the
    # forgetting that lets the folder go, and no row is left of it.
    assert ran(computer, "land:0") == ["changed", "revisions", "pickup", "forget"] and ran(computer, "land:0:2") == []
    assert await records(api, thread) == [] and kept(computer) == {} and computer.app.places.holder is None
    assert seen(computer.folder) == yours and (computer.app.places.copy(str(thread.id)) / "Budget.xlsx").read_text() == "Total,42\n"
    [report] = await told(api, master)
    too_many(report)
    # The thread's own file, not yours: the landing names what it changed before it picked anything up.
    assert files_of(report) == [("Budget.xlsx", "not_merged", None)]
    [end] = await events_of(api, thread.id, EventType.SESSION_COMPLETE)
    assert end.data["saved"] is False


@pytest.mark.slow
async def test_a_folder_of_more_files_than_half_the_cap_that_you_renamed_lands_and_its_history_lists_the_move_as_yours(api, computer, monkeypatch):
    project, master, thread = await begun_with_copy(api, computer)
    many = HISTORY_CAP // 2 + 1
    await woken(api, monkeypatch, thread, [BUDGET, ADD, _final_response("Done.")], during=adding(computer.folder, many, {}))
    [first] = await records(api, thread)
    assert first.saga_state == "completed" and len(first.picked_up) == many
    # You rename the folder of photos: the folder is within the cap, and the rename is two of your changes a photo.
    (computer.folder / "Photos" / "2026").rename(computer.folder / "Photos" / "2026 trip")
    yours = seen(computer.folder)
    await woken(api, monkeypatch, thread, [NOTES, _final_response("Done.")], said="Notes, please.")
    assert (computer.folder / "Notes.md").read_text() == "n\n"
    assert {path: entry for path, entry in seen(computer.folder).items() if path != "Notes.md"} == yours
    _, second = await records(api, thread)
    assert second.saga_state == "completed" and [f["path"] for f in second.files] == ["Notes.md"]
    assert sorted((p["path"], p["before"] is None, p["after"] is None) for p in second.picked_up) == sorted([
        *((path, False, True) for path in photos(many)), *((path, True, False) for path in photos(many, "Photos/2026 trip")),
    ])
    assert await history_of(api, project, computer, "Photos/2026 trip/IMG_000007.jpg") == [("added", {"kind": "you"})]
    assert await history_of(api, project, computer, "Photos/2026/IMG_000007.jpg") == [("deleted", {"kind": "you"}), ("added", {"kind": "you"})]
    _, report = await told(api, master)
    assert files_of(report) == [("Notes.md", "landed", None)] and "landing" not in report.data


@pytest.mark.slow
async def test_a_folder_of_thirty_thousand_files_you_deleted_and_another_you_added_land_as_yours(api, computer, monkeypatch):
    project, master, thread = await begun_with_copy(api, computer)
    await woken(api, monkeypatch, thread, [BUDGET, ADD, _final_response("Done.")], during=adding(computer.folder, 30_000, {}))
    # You delete the folder of thirty thousand photos, and add another of twenty-five thousand scans.
    shutil.rmtree(computer.folder / "Photos")
    yours = you_add(computer.folder, 25_000, "Scans")
    await woken(api, monkeypatch, thread, [NOTES, _final_response("Done.")], said="Notes, please.")
    assert {path: entry for path, entry in seen(computer.folder).items() if path != "Notes.md"} == yours
    assert not (computer.folder / "Photos").exists() and (computer.folder / "Notes.md").read_text() == "n\n"
    _, second = await records(api, thread)
    assert second.saga_state == "completed"
    assert [(p["path"], p["after"] is None) for p in second.picked_up] == [
        *((path, True) for path in photos(30_000)), *((path, False) for path in photos(25_000, "Scans")),
    ]
    assert await history_of(api, project, computer, "Photos/2026/IMG_029999.jpg") == [("deleted", {"kind": "you"}), ("added", {"kind": "you"})]
    assert await history_of(api, project, computer, "Scans/IMG_000000.jpg") == [("added", {"kind": "you"})]


@pytest.mark.slow
async def test_your_edits_in_more_parts_than_one_landing_asks_are_refused_in_their_own_words_and_nothing_of_them_is_recorded(api, computer, monkeypatch):
    # One file a part: names longer than a part holds.
    monkeypatch.setattr(local_history, "_PICKED_CHARS", 10)
    _, master, thread = await begun_with_copy(api, computer)
    await woken(api, monkeypatch, thread, [BUDGET, _final_response("Done.")])
    [end] = await events_of(api, thread.id, EventType.SESSION_COMPLETE)
    # Between two turns, you save files whose names are long, more of them than one landing asks parts for.
    (computer.folder / "Long").mkdir()
    for n in range(local_landing_module.PARTS_MOST + 4):
        (computer.folder / "Long" / f"{'n' * 200}-{n:02d}.md").write_text(f"long {n}\n")
    yours = seen(computer.folder)
    await woken(api, monkeypatch, thread, [NOTES, _final_response("Done.")], said="Notes, please.")
    # As many answers as one landing asks, the pickup's own among them; then nothing but the forgetting.
    assert ran(computer, f"land:{end.id}") == ["changed", "revisions", "pickup", *["picked"] * (local_landing_module.PARTS_MOST - 1), "forget"]
    assert ran(computer, f"land:{end.id}:2") == []
    assert [row.saga_state for row in await records(api, thread)] == ["completed"]
    assert seen(computer.folder) == yours and kept(computer) == {} and computer.app.places.holder is None
    _, report = await told(api, master)
    too_many(report)
    # Its own file alone: not yours, which its turn's open brought into its copy.
    assert files_of(report) == [("Notes.md", "not_merged", None)]


async def test_a_part_its_computer_answers_too_large_is_told_as_yours_and_leaves_nothing_of_the_landing(api, computer, monkeypatch):
    monkeypatch.setattr(local_history, "_PICKED_CHARS", SMALL_PART)
    _, master, thread = await begun_with_copy(api, computer)
    yours: dict = {}
    lie_at(computer, "picked", lambda frame, outcome: {"error": {
        "type": "too_large", "message": "The operation ran, but its result is too large to send. Check what it did before repeating it.",
    }})
    await woken(api, monkeypatch, thread, [BUDGET, ADD, _final_response("Done.")], during=adding(computer.folder, 20, yours))
    assert ran(computer, "land:0") == ["changed", "revisions", "pickup", "picked", "forget"] and ran(computer, "land:0:2") == []
    assert await records(api, thread) == [] and kept(computer) == {} and seen(computer.folder) == yours
    [report] = await told(api, master)
    too_many(report)
    assert files_of(report) == [("Budget.xlsx", "not_merged", None)]


# -- a landing cut at any part of its pickup --------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize("cut", ["after its pickup", "after its first part", "after its last part", "while a part waits"])
async def test_a_landing_lost_at_any_part_of_its_pickup_is_finished_by_the_next_landing_and_your_edits_are_recorded_once_as_yours(
    api, computer, monkeypatch, cut,
):
    monkeypatch.setattr(local_history, "_PICKED_CHARS", SMALL_PART)
    lost_master, lost, next_master, next_ = await two_threads(api, monkeypatch, computer)
    yours = you_add(computer.folder, 20)
    if cut == "while a part waits":
        frames = await killed_while_it_waits(
            api, monkeypatch, computer, lost, lambda frame: frame["session_id"] == str(lost.id) and frame["args"].get("action") == "picked",
        )
        assert [frame["args"]["action"] for frame in frames] == ["picked"]
    else:
        with monkeypatch.context() as first_run:
            lost_after(first_run, *{"after its pickup": ("pickup", 1), "after its first part": ("picked", 1), "after its last part": ("picked", 2)}[cut])
            with pytest.raises(Lost):
                await woken(api, monkeypatch, lost, [EDITS, _final_response("Done.")])
    # Cut before its commit: nothing of the lost landing is in the folder, and none of your edits is on main.
    [row] = await records(api, lost)
    assert row.saga_state == "running" and seen(computer.folder) == yours
    await lands_next(api, monkeypatch, computer, next_)
    # The next landing settled it first: nothing to put back, its parts closed, and what it held let go.
    [settled] = await records(api, lost)
    assert (settled.saga_state, settled.picked_up) == ("compensated", [])
    assert asked_by(computer, next_, settles(lost)) == ["forget"] and await left_open(api, lost) == []
    # Then it landed its own, your edits picked up in parts of its own: each recorded once, as yours.
    assert asked_by(computer, next_, "land:0") == ["changed", "revisions", "pickup", "picked", "picked", "commit", "apply", "record", "forget"]
    [own] = await records(api, next_)
    assert [p["path"] for p in own.picked_up] == photos(20) and [f["path"] for f in own.files] == ["Y.md"]
    for workstream in (lost.config["workstream_id"], next_.config["workstream_id"]):
        assert all(times == 1 for times in (await recorded_as_yours(api, UUID(workstream))).values())
    assert {path: entry for path, entry in seen(computer.folder).items() if path != "Y.md"} == yours
    assert kept(computer) == {} and computer.app.places.holder is None


async def test_a_landing_taken_up_again_after_a_part_of_its_pickup_hears_the_parts_it_had_from_the_journal_and_lands(api, computer, monkeypatch):
    monkeypatch.setattr(local_history, "_PICKED_CHARS", SMALL_PART)
    project, master, thread = await begun_with_copy(api, computer)
    yours: dict = {}
    with monkeypatch.context() as first_run:
        lost_after(first_run, "picked", 1)
        with pytest.raises(Lost):
            await woken(api, monkeypatch, thread, [BUDGET, ADD, _final_response("Done.")], during=adding(computer.folder, 20, yours))
    # Taken up again, its model answering with no step: the landing goes on from the journal.
    await woken(api, monkeypatch, thread, [_final_response("Done.")], said="Is it done?")
    # Its computer heard each step once: the parts it had answered were answered again from the journal.
    assert ran(computer, "land:0") == ["changed", "revisions", "pickup", "picked", "picked", "commit", "apply", "record", "forget"]
    [row] = await records(api, thread)
    assert row.saga_state == "completed" and [p["path"] for p in row.picked_up] == photos(20)
    assert (computer.folder / "Budget.xlsx").read_text() == "Total,42\n"
    assert {path: entry for path, entry in seen(computer.folder).items() if path != "Budget.xlsx"} == yours
    assert await history_of(api, project, computer, "Photos/2026/IMG_000013.jpg") == [("added", {"kind": "you"})]
    assert kept(computer) == {} and computer.app.places.holder is None


@pytest.mark.slow
async def test_a_landing_that_recorded_and_whose_row_was_written_short_of_its_parts_is_completed_with_every_part_as_yours(
    api, computer, monkeypatch,
):
    monkeypatch.setattr(local_history, "_PICKED_CHARS", SMALL_PART)
    lost_master, lost, next_master, next_ = await two_threads(api, monkeypatch, computer)
    you_add(computer.folder, 20)
    with monkeypatch.context() as first_run:
        lost_after(first_run, "record", 1)
        with pytest.raises(Lost):
            await woken(api, monkeypatch, lost, [EDITS, _final_response("Done.")])
    with monkeypatch.context() as again:
        # Taken up again, its turn writes the steps it has asked again so far, and is lost once more at its pickup.
        again.setattr(landing_module, "_ROW_EVERY", 0)
        lost_after(again, "pickup", 1)
        with pytest.raises(Lost):
            await woken(api, monkeypatch, lost, [_final_response("Done.")])
    [row] = await records(api, lost)
    assert [s["tool_name"] for s in row.steps] == ["history.pickup"]
    await lands_next(api, monkeypatch, computer, next_)
    # Its record is in the journal, and so is each part of its pickup: complete, your edits recorded as yours, every part.
    [settled] = await records(api, lost)
    assert settled.saga_state == "completed" and asked_by(computer, next_, settles(lost)) == ["fetch", "forget"]
    assert [p["path"] for p in settled.picked_up] == photos(20)
    [own] = await records(api, next_)
    assert (own.saga_state, own.picked_up) == ("completed", [])
    assert all(times == 1 for times in (await recorded_as_yours(api, UUID(lost.config["workstream_id"]))).values())
    assert kept(computer) == {}


# -- parts that do not go on from the last ----------------------------------------------------------------------------


def _twice(frame, outcome):
    """The answer names its second file as its first again: one of your edits twice, and one not at all."""
    part = outcome["ok"]["picked_up"]
    return {"ok": {**outcome["ok"], "picked_up": [part[0], part[0], *part[2:]]}}


def _again(frame, outcome):
    """The last part names again the file the part before it ended at, in place of its own last."""
    part = outcome["ok"]["picked_up"]
    if outcome["ok"]["more"]:
        return None
    return {"ok": {**outcome["ok"], "picked_up": [{"path": frame["args"]["after"], "before": None, "after": "a" * 40}, *part[:-1]]}}


def _fewer(frame, outcome):
    """A part says none are left after it, where some are: the parts would stop short of your edits."""
    return {"ok": {**outcome["ok"], "more": 0}} if outcome["ok"]["more"] else None


_EMPTY: list[int] = []


def _none(frame, outcome):
    """The first part after the pickup's names no file, and says as many are left as the pickup did; honest after it."""
    _EMPTY.append(1)
    if len(_EMPTY) > 1:
        return None
    return {"ok": {"picked_up": [], "more": outcome["ok"]["more"] + len(outcome["ok"]["picked_up"])}}


def _no_pickup(frame, outcome):
    """The pickup names no commit of your edits, and says more of them are left."""
    return {"ok": {**outcome["ok"], "commit": None}}


def _no_first(frame, outcome):
    """The pickup names none of your edits, and says all of them are left."""
    return {"ok": {**outcome["ok"], "picked_up": [], "more": outcome["ok"]["more"] + len(outcome["ok"]["picked_up"])}}


@pytest.mark.parametrize(("action", "lie"), [
    ("pickup", _twice), ("picked", _twice), ("picked", _again), ("picked", _fewer), ("picked", _none), ("pickup", _no_pickup),
    ("pickup", _no_first),
], ids=["the pickup names a file twice", "a part names a file twice", "a part names the file the last ended at", "a part says none are left",
        "a part names none", "the pickup names no commit", "the pickup names none"])
async def test_a_pickup_whose_parts_do_not_go_on_from_the_last_is_no_answer_and_none_of_your_edits_is_recorded_twice(
    api, computer, monkeypatch, action, lie,
):
    monkeypatch.setattr(local_history, "_PICKED_CHARS", SMALL_PART)
    _EMPTY.clear()
    project, master, thread = await begun_with_copy(api, computer)
    yours: dict = {}
    lie_at(computer, action, lie)
    await woken(api, monkeypatch, thread, [BUDGET, ADD, _final_response("Done.")], during=adding(computer.folder, 20, yours))
    # Its first landing took nothing of it: no commit, no apply, nothing recorded; the second landed, honestly answered.
    asked = ran(computer, "land:0")
    assert "commit" not in asked and asked[-1] == "forget"
    first, landed = await records(api, thread)
    assert (first.saga_state, first.commit, first.picked_up, landed.saga_state) == ("compensated", None, [], "completed")
    # Its row says which answer was none, and why.
    assert [(s["tool_name"], s["error"]) for s in first.steps if s["state"] == "failed"] == [
        ("history.pickup", "This computer's answer to 'pickup' names your edits otherwise than a pickup does") if action == "pickup"
        else ("history.picked", "This computer's answer to 'picked' does not go on from the part before it"),
    ]
    assert [p["path"] for p in landed.picked_up] == photos(20)
    assert all(times == 1 for times in (await recorded_as_yours(api, UUID(project["id"]))).values())
    assert {path: entry for path, entry in seen(computer.folder).items() if path != "Budget.xlsx"} == yours
    [report] = await told(api, master)
    assert files_of(report) == [("Budget.xlsx", "landed", None)]
    assert kept(computer) == {} and computer.app.places.holder is None
