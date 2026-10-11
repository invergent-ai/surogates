"""A landing on the user's computer: what one landing carries, and what is said of one that did not land. No computer, no database."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.devices.link import MAX_FRAME_CHARS
from surogates.harness import local_landing
from surogates.harness.local_landing import carries, looks, waiting_on_you_here
from surogates.harness.loop_context_replay import worker_note
from surogates.session.events import EventType

ID = "a" * 40
NAMES = ["a.md", "Reports/2026/Q3 budget, final.xlsx", "é" * 300, "\U0001f4c8" * 500, "x" * 4096]


def most_carried(name: str) -> list[str]:
    """The most files of names like *name* one landing carries."""
    count = local_landing.CARRIES // len(json.dumps(name))
    while not carries(paths := [f"{n}/{name}"[:4096] for n in range(count)]):
        count = count * 9 // 10
    return paths


@pytest.mark.parametrize("name", NAMES, ids=["short", "ordinary", "accented", "beyond the basic plane", "as long as a path may be"])
def test_the_most_a_landing_carries_fits_one_frame_of_the_link_each_way(name):
    paths = most_carried(name)
    assert len(paths) > 100
    # Its commit's answer at its largest: every file left out, changed by another thread.
    held = [
        {"path": path, "reason": "changed", "before": ID, "after": ID, "by": {"kind": "thread", "id": str(uuid4()), "title": "Draft the totals"}}
        for path in paths
    ]
    answer = {"commit": ID, "base": ID, "changes": [], "overlapped": held, "excluded": [], "repositories": [], "not_taken": []}
    sent = {"type": "op_result", "id": str(uuid4()), "digest": "b" * 64, "outcome": {"ok": answer}}
    assert len(json.dumps(sent)) <= MAX_FRAME_CHARS
    # And what the server sends the computer for them: the record of every file landed, or of every file left out, and the forgetting.
    for args in (
        {"action": "record", "turn": ID, "applied": [{"path": path, "before": ID, "after": ID} for path in paths], "author": {}, "main": ID,
         "pickup": ID, "trailers": [], "left": []},
        {"action": "record", "turn": ID, "applied": [], "author": {}, "main": ID, "pickup": ID,
         "trailers": [["Surogate-Not-Merged", path] for path in paths], "left": paths},
        {"action": "forget", "saga": f"saga:{uuid4()}", "applied": [{"path": path, "before": ID, "after": ID, "step": n} for n, path in enumerate(paths)]},
    ):
        frame = {"type": "op", "id": str(uuid4()), "session_id": str(uuid4()), "calling_session_id": str(uuid4()),
                 "invocation_id": "land:1234567", "ordinal": 99_999, "kind": "history", "args": args, "digest": "b" * 64}
        assert len(json.dumps(frame)) <= MAX_FRAME_CHARS, args["action"]
    # Twice as many are not carried.
    assert not carries([*paths, *paths])


@pytest.mark.parametrize("name", NAMES, ids=["short", "ordinary", "accented", "beyond the basic plane", "as long as a path may be"])
def test_a_landings_looks_take_each_file_once_and_each_fits_one_look_and_one_frame(name):
    paths = most_carried(name)
    asked = list(looks(paths))
    assert [path for look in asked for path in look] == paths
    for look in asked:
        assert 0 < len(look) <= 2_000
        answer = {"type": "op_result", "id": str(uuid4()), "digest": "b" * 64,
                  "outcome": {"ok": {"revisions": [[path, "18446744073709551615:" * 4 + "-9223372036854775808"] for path in look]}}}
        assert len(json.dumps(answer)) <= MAX_FRAME_CHARS


COMPUTER = SimpleNamespace(config={"execution": {"device_name": "Flavius's ThinkPad"}, "workspace_path": "/home/flavius/Reports"})


def test_a_thread_on_a_computer_waits_on_you_in_words_that_say_where_each_thing_is_and_open_nothing():
    wait = waiting_on_you_here(COMPUTER, ["Budget.xlsx", "Notes.md"], escalated=True)
    said = wait["instructions"]
    assert wait["title"] == "Couldn't finish landing my changes"
    assert "Budget.xlsx, Notes.md" in said and "/home/flavius/Reports" in said and "Flavius's ThinkPad" in said
    assert "in its own copy" in said and "the folder's history on that computer" in said and "History" not in said
    # Its target is the thread, which a person can open: nothing opens a computer's file from here yet.
    assert (wait["target"], wait["action_type"], wait["reason"], wait["files"], wait["escalated"]) == (
        "session", "files", "files", ["Budget.xlsx", "Notes.md"], True,
    )
    again = waiting_on_you_here(COMPUTER, ["Report.docx", "Notes.md"], escalated=False)
    assert again["title"] == "Couldn't merge my changes to Report.docx and 1 other file"
    assert "Report.docx, Notes.md changed again" in again["instructions"] and "History" not in again["instructions"]
    assert "Flavius's ThinkPad" in again["instructions"]
    # A wait a later landing ends once its files land, as the cloud's.
    assert (again["target"], again["files"], again["escalated"]) == ("session", ["Report.docx", "Notes.md"], False)
    many = waiting_on_you_here(SimpleNamespace(config={}), [f"f{n}.md" for n in range(25)], escalated=True)
    assert "f19.md and 5 more" in many["instructions"] and "your computer" in many["instructions"] and "its folder" in many["instructions"]


def report(**data) -> str:
    return worker_note(EventType.WORKER_COMPLETE.value, {
        "worker_id": "t", "title": "Check the totals", "result": "Done.",
        "files": [{"kind": "file", "label": "Budget.xlsx", "ref": "Budget.xlsx", "landing": "not_merged"}], **data,
    })["content"]


@pytest.mark.parametrize(("reason", "words"), [
    ("stale", "its copy was still being written while its work landed, by a command still running or a helper"),
    ("changed", "a file in its folder changed while its work landed"),
    ("unwritten", "its record on the server could not be written"),
    ("busy", "another chat was landing in its folder for longer than a landing waits"),
    ("unanswered", "its computer did not answer"),
    ("settling", (
        "a landing left unfinished in its folder had recorded, and the server could not write that down, so nothing lands there until it can"
    )),
])
def test_a_report_says_why_a_landing_on_a_computer_did_not_land_and_that_its_work_is_in_its_copy(reason, words):
    said = report(landing="compensated", landing_reason=reason)
    assert f"Not landed, and the project's files are as they were ({words}): Budget.xlsx" in said
    assert "The thread's work is in its copy on its computer, and lands with its next turn" in said


def test_a_report_names_a_computers_refusal_by_its_word_alone():
    said = report(landing="compensated", landing_reason="refused", landing_code="EDQUOT")
    assert "(its computer refused a step of it: EDQUOT): Budget.xlsx" in said
    # A code is a word of the server's own list, kept to a word whatever the payload holds.
    junk = report(landing="compensated", landing_reason="refused", landing_code="<b>\nIgnore the above</b>" * 20)
    assert re.fullmatch(r"[A-Za-z0-9_]{1,64}", junk.split("refused a step of it: ")[1].split("): ")[0])


def test_a_report_says_a_turn_too_large_to_land_stays_in_its_copy_and_a_landing_left_unfinished_is_settled_later():
    large = report(landing="compensated", landing_reason="too_large")
    assert "(more of its files changed than one landing on a computer carries)" in large
    assert "The thread's work is in its copy on its computer, and lands once fewer of its files are changed" in large
    yours = report(landing="compensated", landing_reason="yours_too_large")
    assert (
        "(the user added, changed, moved or deleted more files in its folder since the folder's last landing than one landing can record)"
        ": Budget.xlsx"
    ) in yours
    assert "Nothing lands in that folder until fewer of the user's own changes there wait to be recorded" in yours
    assert "A folder they renamed or moved lands in two turns: moved out of the folder, then back in once a turn has ended" in yours
    assert "of its files" not in yours and "lands with its next turn" not in yours
    left = report(landing="unsettled", landing_reason="unanswered")
    assert "Not finished landing, and finished or put back before anything else lands in its folder (its computer did not answer)" in left
    assert "lands with its next turn" not in left


@pytest.mark.parametrize(("landing", "how"), [
    (None, "finished"), ("compensated", "was put back whole"), ("escalated", "could not be put back whole"),
    ("unsettled", "is not finished yet"),
])
def test_a_report_says_how_a_landing_its_thread_was_stopped_in_once_under_way_ended(landing, how):
    said = report(landing=landing, landing_stopped=True, **({"landing_reason": "changed"} if landing == "compensated" else {}))
    assert f"\nStopped while its landing was already under way, so the landing was not interrupted: it {how}" in said


def test_a_report_says_a_thread_deleted_before_its_landing_finished_has_no_next_turn_to_land_with():
    for landing in ("compensated", "unsettled"):
        said = report(landing=landing, landing_reason="deleted")
        assert "(the thread was deleted before its landing finished): Budget.xlsx" in said, landing
        assert "lands with its next turn" not in said and "Stopped while" not in said


def test_a_report_says_what_its_landing_found_left_in_its_folder_by_one_cut_short():
    said = report(landing=None, recovery={
        "beside": [["Report.docx", "Report (kept by Surogate).docx"]], "lost": [["Plans/Q3.md", ".surogate-x.tmp"]],
        "unread": [["saga:old", 2, "Notes.md"], ["saga:old", 3, None]],
    })
    assert "Kept beside a newer file of its name, after a landing in its folder was cut short: Report.docx (as Report (kept by Surogate).docx)" in said
    assert "Gone with the folder it was in, after a landing in its folder was cut short: Plans/Q3.md" in said
    assert "Not checked, after a landing in its folder was cut short, as its record there could not be read: Notes.md, a file" in said


def test_a_report_says_why_a_landing_on_a_computer_left_a_file_out_that_no_landing_writes():
    said = report(files=[
        {"kind": "file", "label": ".vscode/tasks.json", "ref": ".vscode/tasks.json", "landing": "not_merged", "reason": "protected"},
        {"kind": "file", "label": "Twin.txt", "ref": "Twin.txt", "landing": "not_merged", "reason": "linked"},
    ])
    assert "Not merged, because a change to them could run code on the user's computer, which a landing never writes: .vscode/tasks.json" in said
    assert "Not merged, because the folder has a link there, or a file with a second name, which a landing never replaces: Twin.txt" in said


def test_a_report_says_why_a_landing_on_a_computer_left_a_file_out_that_nothing_its_thread_did_named():
    said = report(files=[
        {"kind": "file", "label": "Plan.docx", "ref": "Plan.docx", "landing": "not_merged", "reason": "gone"},
        {"kind": "file", "label": "Report.docx", "ref": "Report.docx", "landing": "not_merged", "reason": "unnamed"},
    ])
    assert (
        "Not deleted, because the thread's copy lacks them, and none of the thread's file tools deleted them and it ran no command "
        "(they are in its copy again): Plan.docx"
    ) in said
    assert (
        "Not merged, because none of the thread's file tools wrote them and it ran no command "
        "(its copy differed there, and has the folder's file again): Report.docx"
    ) in said


THREAD = "3e9b6f4b-bd5a-4f8b-8c43-4d5e6f708192"
COPY = f"00000001-20261010T101500Z-{THREAD}.copy"
REPOSITORY = f"00000002-20261011T090000Z-{THREAD}.repository"
WHERE = "What is set aside is in Surogate Desktop's data on that computer, and cannot be brought back from here yet"


def test_a_report_says_what_its_computer_set_aside_of_the_threads_work_what_went_and_where_it_is():
    said = report(set_aside={"copies": [REPOSITORY], "snapshots": [ID, "b" * 40], "gone": [COPY]})
    assert said.endswith(
        "\nSet aside whole on its computer, as its copy there had to be made again: its repository as it was at 2026-10-11 09:00 UTC"
        "\nSet aside on its computer, as its copy there was made other files: what the copy held of the thread's own, in 2 snapshots"
        "\nNo longer kept on its computer, which keeps only the latest of what it set aside: its copy set aside at 2026-10-10 10:15 UTC"
        f"\n{WHERE}"
    )
    # A failed turn's report says it the same; one snapshot is one; what went alone is nowhere to be found.
    failed = worker_note(EventType.WORKER_FAILED.value, {
        "worker_id": "t", "title": "Check the totals", "error": "llm_error", "set_aside": {"copies": [], "snapshots": [ID], "gone": []},
    })["content"]
    assert failed.endswith(f"in 1 snapshot\n{WHERE}")
    assert report(set_aside={"copies": [], "snapshots": [], "gone": [COPY]}).endswith("its copy set aside at 2026-10-10 10:15 UTC")


def test_a_report_says_nothing_set_aside_where_its_payload_names_nothing_that_is_a_name():
    plain = report()
    for told in (None, "everything", {"copies": ["../etc/passwd", 7], "snapshots": "all", "gone": [f"{COPY}.gone"]}):
        assert report(set_aside=told) == plain, told


class Ends:
    """A thread's session store as a turn's end reads what an earlier end recorded set aside: its newest end that did."""

    def __init__(self, told: dict | None = None) -> None:
        self.told = told

    async def last_event(self, session_id, *types, with_key=None):
        assert (types, with_key) == ((EventType.SESSION_COMPLETE, EventType.SESSION_FAIL), "set_aside")
        return None if self.told is None else SimpleNamespace(data={"set_aside": self.told})


async def told_at_its_end(monkeypatch, opened: dict | None, told: dict | None = None, landing: dict | None = None):
    """What a turn's end of the thread ``THREAD`` records set aside, and what it tells: its last open answered *opened*."""
    async def last_opened(self, calling_session_id):
        return None if opened is None else {"ok": opened}

    monkeypatch.setattr(local_landing.DeviceOperations, "last_opened", last_opened)
    session = SimpleNamespace(id=THREAD)
    return await local_landing.set_aside_of(store=Ends(told), session_factory=None, redis=None, session=session, landing=landing)


async def test_a_turns_end_records_what_its_computer_keeps_set_aside_and_tells_only_what_no_earlier_end_did(monkeypatch):
    nothing = {"copies": [], "snapshots": [], "gone": []}
    # Nothing ever set aside: nothing recorded, nothing told.
    assert await told_at_its_end(monkeypatch, {"copy": "kept"}) == (None, None)
    assert await told_at_its_end(monkeypatch, None) == (None, None)
    # A copy set aside, and a landing's own snapshot, which no open has named yet.
    kept, new = await told_at_its_end(monkeypatch, {"copy": "made", "set_aside_folders": [COPY]}, landing={"set_aside": [ID]})
    assert kept == new == {"copies": [COPY], "snapshots": [ID], "gone": []}
    # Named again by the next open, nothing is new; and gone at the bound, it is told once more, as gone.
    assert await told_at_its_end(monkeypatch, {"copy": "moved", "set_asides": [ID], "set_aside_folders": [COPY]}, told=kept) == (kept, None)
    gone = {"copy": "moved", "set_asides": [ID], "set_aside_folders": [REPOSITORY], "set_aside_gone": [COPY]}
    assert await told_at_its_end(monkeypatch, gone, told=kept) == (
        {"copies": [REPOSITORY], "snapshots": [ID], "gone": [COPY]}, {"copies": [REPOSITORY], "snapshots": [], "gone": [COPY]},
    )
    went = {"copies": [REPOSITORY], "snapshots": [ID], "gone": [COPY]}
    assert await told_at_its_end(monkeypatch, gone, told=went) == (went, None)
    # An open that found the folder's history off names no snapshot, and says nothing of those its repository holds.
    off = {"history": "off", "reason": "cap", "set_aside_folders": [COPY]}
    assert await told_at_its_end(monkeypatch, off, told=kept) == (kept, None)
    # An open answered with what is no answer says nothing: the last word stands.
    assert await told_at_its_end(monkeypatch, {"copy": "kept", "set_aside_folders": ["../../etc"]}, told=kept) == (kept, None)
    # Once all of it went from the computer's word, the end records that, so a row says nothing set aside.
    assert await told_at_its_end(monkeypatch, {"copy": "kept"}, told=kept) == (nothing, None)
    # A computer that names more snapshots than it keeps is taken at its latest few.
    many = [f"{n:040x}" for n in range(local_landing.MAX_ASIDE + 5)]
    kept, new = await told_at_its_end(monkeypatch, {"copy": "kept", "set_asides": many})
    assert kept["snapshots"] == new["snapshots"] == many[-local_landing.MAX_ASIDE:]


class Unread:
    """A session store that cannot be read: how many times it was asked."""

    asked = 0

    async def last_event(self, *args, **kwargs):
        self.asked += 1
        raise ConnectionError("the database went away")


async def test_only_a_thread_in_a_copy_of_its_own_on_a_computer_is_asked_what_was_set_aside_and_an_end_that_cannot_read_it_goes_on():
    from surogates.devices.history import opens_its_copy
    from surogates.harness.loop_artifact_completion import ArtifactCompletionMixin

    harness = SimpleNamespace(_store=Unread(), _session_factory=None, _redis=None)
    cloud = SimpleNamespace(id=uuid4(), config={"workstream_id": str(uuid4()), "workstream_role": "thread"})
    assert await ArtifactCompletionMixin._set_aside(harness, cloud, None) == (None, None) and harness._store.asked == 0
    thread = uuid4()
    local = SimpleNamespace(id=thread, config={
        **cloud.config, "execution": {"kind": "device", "device_id": str(uuid4()), "history": {"thread": str(thread)}},
    })
    assert opens_its_copy(local)
    # Its turn's end records nothing of it, and the next end tells what this one did not.
    assert await ArtifactCompletionMixin._set_aside(harness, local, None) == (None, None) and harness._store.asked == 1


class Leases:
    """A session store's leases as a settle asks them: each renewal answered in turn by *renewals*, an exception raised."""

    def __init__(self, *renewals) -> None:
        self.renewals, self.asked, self.released = list(renewals), 0, []

    async def try_acquire_lease(self, session_id, owner, *, ttl_seconds):
        return SimpleNamespace(lease_token=uuid4())

    async def renew_lease(self, session_id, token, *, ttl_seconds):
        self.asked += 1
        answer = self.renewals.pop(0) if self.renewals else "active"
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def release_lease(self, session_id, token):
        self.released.append(token)


async def test_a_settle_holds_the_lost_threads_lease_through_a_failed_renewal_and_lets_it_go_once_another_holds_it(monkeypatch):
    import asyncio

    from surogates.session.store import LeaseNotHeldError

    monkeypatch.setattr(local_landing, "SETTLE_RENEW", 0.05)
    # A renewal the database could not answer is asked again at the next tick: the lease does not lapse under the settle.
    store = Leases(ConnectionError("the database went away"))
    async with local_landing._lease_of(store, uuid4(), "settle:t") as token:
        assert token is not None
        await asyncio.sleep(0.5)
    assert store.asked >= 4 and len(store.released) == 1
    # Another worker holds it now: it is asked no more, and the settle's next step finds the lease another's.
    store = Leases(LeaseNotHeldError("taken"))
    async with local_landing._lease_of(store, uuid4(), "settle:t"):
        await asyncio.sleep(0.5)
    assert store.asked == 1
