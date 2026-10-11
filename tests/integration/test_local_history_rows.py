"""What a project thread's work on the user's computer leaves for a person to read: a file's History, the thread's row, and its report.

Each scene runs a worker's real wake on the tests' computer, whose app runs
the folder's real history.  A file in a folder on a computer lists its
versions from that folder's records, to its own user alone, and none of
them is opened, restored or undone from here; a folder whose history is
off says so where its History is asked for.  A thread's row marks the files
its landings changed there, and names no landing to undo.  What its
computer set aside of its work, out of its copy, and what of that went at
the computer's bound, is told in the thread's report once, and stands on its
row.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from surogates.harness import loop_artifact_completion as completion
from surogates.sandbox import local_history
from surogates.session.events import EventType
from tests.test_steer_loop import _final_response

from .test_devices import add_user, api, link_url, register  # noqa: F401  (api and link_url are fixtures)
from .test_file_history import history_of, opened, restored, undone
from .test_local_history_keep import FAILS
from .test_local_history_landing import EDITS, lie_at, records, said, seen, told
from .test_local_history_open import begun_with_copy, woken
from .test_local_history_threads import computer  # noqa: F401  (a fixture)
from .test_turn_sagas import calling
from .test_workstream_overview import rows

pytestmark = pytest.mark.asyncio(loop_scope="session")


#: A turn that writes one file, and its answer.
NOTES = calling(("write_file", {"path": "Notes.md", "content": "notes\n"}))
#: What a report says of a thread's work its computer set aside, and where that is.
WHOLE = "Set aside whole on its computer, as its copy there had to be made again"
SNAPSHOTS = "Set aside on its computer, as its copy there was made other files"
GONE = "No longer kept on its computer, which keeps only the latest of what it set aside"
WHERE = "What is set aside is in Surogate Desktop's data on that computer, and cannot be brought back from here yet"


async def history_on(api, project: dict, computer, path: str, token: str | None = None):
    """The response to asking for *path*'s History in the folder on *computer*."""
    return await api.client.get(
        f"/v1/workstreams/{project['id']}/history", params={"path": path, "device_id": computer.device_id}, headers=api.auth(token),
    )


# -- a computer's file, and its thread's row -----------------------------------------------------------------------


async def test_a_computers_file_lists_its_history_to_its_own_user_and_none_of_it_is_opened_from_here(api, computer, monkeypatch):
    project, master, thread = await begun_with_copy(api, computer)
    report = computer.folder / "Report.docx"

    def you_save(frame, outcome) -> None:
        report.write_bytes(b"PK report v2, by you")

    lie_at(computer, "revisions", you_save)
    await woken(api, monkeypatch, thread, [EDITS, _final_response("Edited the report.")])
    listed = await history_on(api, project, computer, "Report.docx")
    assert listed.status_code == 200, listed.text
    # Newest first: the thread's version, which did not land; yours, which its landing picked up; and the file as it first was.
    assert [(v["by"]["kind"], v["change"], v["merged"], v["available"]) for v in listed.json()] == [
        ("thread", "changed", False, False), ("you", "changed", True, False), ("you", "added", True, False),
    ]
    assert listed.json()[0]["by"] == {"kind": "thread", "thread_id": str(thread.id), "title": "Check the totals"}
    # None of it is the cloud's, and no route opens, restores or undoes it from here: its computer is asked nothing.
    before, folder = len(computer.app.received), seen(computer.folder)
    [_, yours, _] = listed.json()
    [row] = await records(api, thread)
    assert await history_of(api, project, "Report.docx") == []
    assert (await opened(api, project, yours["id"], "Report.docx")).status_code == 404
    assert (await restored(api, project, yours, status=404))["detail"] == "No such version."
    assert (await undone(api, project, status=404, landing=str(row.id)))["detail"] == "No such change."
    # An Undo of the thread's changes is of the cloud's files, where it landed none.
    assert (await undone(api, project, thread=str(thread.id)))["applied"] == []
    assert len(computer.app.received) == before and seen(computer.folder) == folder
    assert report.read_bytes() == b"PK report v2, by you"
    # Its row marks the files its landing changed in the folder, and offers no Undo of a landing on a computer.
    [found] = await rows(api, project)
    assert [(f["label"], f["landing"]) for f in found["files"] if f["kind"] == "file"] == [
        ("Report.docx", "redoing"), ("Budget.xlsx", "landed"),
    ]
    assert found["landing_id"] is None
    # Another user of the organization reads none of it: the project is its user's own.
    _, stranger = await add_user(api.app.state.session_factory, api.org_id)
    assert (await history_on(api, project, computer, "Report.docx", token=stranger)).status_code == 404


async def test_a_folder_whose_history_is_off_on_its_computer_says_so_where_its_history_is_asked_for(api, computer, monkeypatch):
    project, _, thread = await begun_with_copy(api, computer)
    await woken(api, monkeypatch, thread, [EDITS, _final_response("Edited the report.")])
    assert [v["change"] for v in (await history_on(api, project, computer, "Report.docx")).json()] == ["changed", "added"]
    # The folder now holds a file whose name its history cannot keep: the thread's next open finds it has none, and
    # names a copy of the thread's it set aside, which only a check that knows whose copy was asked about takes.
    computer.app.places.off = "names"
    aside = f"00000001-20261010T101500Z-{thread.id}.copy"
    computer.app.lie = lambda frame, outcome: (
        {"ok": {**outcome["ok"], "set_aside_folders": [aside]}} if frame["args"].get("action") == "open" and "ok" in outcome else outcome
    )
    await woken(api, monkeypatch, thread, [_final_response("Done.")], said="Go on.")
    off = await history_on(api, project, computer, "Report.docx")
    assert (off.status_code, off.json()["detail"]) == (409, "History is off: a file in this folder has a name that is not UTF-8.")
    # What its computer last said of the folder is read from the journal and checked as an answer given now: an open
    # answered with what is no answer says nothing, and the last answer stands.
    computer.app.lie = lambda frame, outcome: {"ok": {"history": "off", "reason": "<b>because</b>"}}
    await woken(api, monkeypatch, thread, [_final_response("Done.")], said="Go on.")
    assert (await history_on(api, project, computer, "Report.docx")).json()["detail"] == (
        "History is off: a file in this folder has a name that is not UTF-8."
    )
    computer.app.lie, computer.app.places.off = None, "cap"
    await woken(api, monkeypatch, thread, [_final_response("Done.")], said="Go on.")
    assert (await history_on(api, project, computer, "Report.docx")).json()["detail"] == "History is off: this folder has more than 50,000 files."
    # Once its history is back, the folder's versions are listed again.
    computer.app.places.off = None
    await woken(api, monkeypatch, thread, [_final_response("Done.")], said="Go on.")
    assert [v["change"] for v in (await history_on(api, project, computer, "Report.docx")).json()] == ["changed", "added"]
    # The cloud's files are not that folder's: their History is on, and lists none of it.
    assert await history_of(api, project, "Report.docx") == []


async def test_a_folders_history_is_said_off_by_its_own_projects_threads_on_that_computer_alone(api, computer, monkeypatch):
    project, _, thread = await begun_with_copy(api, computer)
    other_project, _, other = await begun_with_copy(api, computer, nonce="second-thread-nonce-0002")
    # Another project's thread on the same computer finds its folder's history off, as its computer answers its open.
    computer.app.lie = lambda frame, outcome: (
        {"ok": {"history": "off", "reason": "cap"}}
        if frame["session_id"] == str(other.id) and frame["args"].get("action") == "open" else outcome
    )
    await woken(api, monkeypatch, thread, [EDITS, _final_response("Edited the report.")])
    await woken(api, monkeypatch, other, [_final_response("Done.")])
    assert (await history_on(api, other_project, computer, "Report.docx")).json()["detail"] == (
        "History is off: this folder has more than 50,000 files."
    )
    assert [v["change"] for v in (await history_on(api, project, computer, "Report.docx")).json()] == ["changed", "added"]
    # A folder on another computer of the user's is that computer's to say: this one's word is none of it.
    elsewhere = await register(api, name="Office iMac")
    listed = await api.client.get(
        f"/v1/workstreams/{other_project['id']}/history", params={"path": "Report.docx", "device_id": elsewhere["id"]}, headers=api.auth(),
    )
    assert (listed.status_code, listed.json()) == (200, [])


# -- what its computer set aside of a thread's work ----------------------------------------------------------------


def cut_from_its_branch(computer, thread, text: str) -> None:
    """*thread*'s copy on *computer* holds *text* in a file no snapshot took, and its repository's index is gone: its
    next open sets the copy aside whole, and makes it again from the thread's branch."""
    (computer.app.places.copy(str(thread.id)) / "unlanded.md").write_text(text)
    (computer.app.places.place / "clones" / str(thread.id) / "worktrees" / str(thread.id) / "index").unlink()


def set_aside_on(computer) -> list[str]:
    """What *computer*'s place keeps set aside whole, and the marks of what went, by name, oldest first."""
    return sorted(path.name for path in (computer.app.places.place / "set-aside").iterdir())


def when(name: str) -> tuple[str, str]:
    """When a copy set aside by *name* was, as a report says it and as a row has it."""
    day, time = name[9:17], name[18:24]
    return (
        f"{day[:4]}-{day[4:6]}-{day[6:]} {time[:2]}:{time[2:4]} UTC",
        f"{day[:4]}-{day[4:6]}-{day[6:]}T{time[:2]}:{time[2:4]}:{time[4:]}Z",
    )


async def reports(api, master) -> list:
    """Every report the thread sent its master, its failures among them, oldest first."""
    return await api.app.state.session_store.get_events(master.id, types=[EventType.WORKER_COMPLETE, EventType.WORKER_FAILED])


async def test_a_copy_its_computer_set_aside_and_one_that_went_at_its_bound_each_reach_the_threads_report_once_and_stand_on_its_row(
    api, computer, monkeypatch,
):
    # A thread keeps one copy set aside on this computer: the next one set aside makes it go.
    monkeypatch.setattr(local_history, "_ASIDE_WHOLE", 1)
    project, master, thread = await begun_with_copy(api, computer)
    await woken(api, monkeypatch, thread, [NOTES, _final_response("Noted.")])
    # Work its copy holds that no landing took, and the copy's index lost: its next open sets the copy aside whole.
    cut_from_its_branch(computer, thread, "the thread's work, 1\n")
    await woken(api, monkeypatch, thread, FAILS, said="Go on.")
    [first] = set_aside_on(computer)
    assert (computer.app.places.place / "set-aside" / first / "unlanded.md").read_text() == "the thread's work, 1\n"
    # The turn failed, and its report says so all the same: once, and its next turn's says nothing of it.
    await woken(api, monkeypatch, thread, [_final_response("Done.")], said="Go on.")
    await woken(api, monkeypatch, thread, [_final_response("Done.")], said="Go on.")
    landed, failed, again, more = await reports(api, master)
    assert (landed.type, failed.type) == (EventType.WORKER_COMPLETE.value, EventType.WORKER_FAILED.value)
    assert [report.data.get("set_aside") for report in (landed, failed, again, more)] == [
        None, {"kept": [{"kind": "copy", "at": when(first)[1]}], "snapshots": 0, "gone": []}, None, None,
    ]
    assert f"\n{WHOLE}: its copy as it was at {when(first)[0]}\n{WHERE}" in said(failed)
    assert all("set aside" not in said(report).lower() for report in (landed, again, more))
    # Set aside once more, the first goes at the computer's bound: the report says each, once.
    cut_from_its_branch(computer, thread, "the thread's work, 2\n")
    await woken(api, monkeypatch, thread, [_final_response("Done.")], said="Go on.")
    await woken(api, monkeypatch, thread, [_final_response("Done.")], said="Go on.")
    second = next(name for name in set_aside_on(computer) if name.endswith(".copy"))
    assert set_aside_on(computer) == [f"{first}.gone", second]
    *_, gone, after = await reports(api, master)
    assert (gone.data["set_aside"], after.data.get("set_aside")) == (
        {"kept": [{"kind": "copy", "at": when(second)[1]}], "snapshots": 0, "gone": [{"kind": "copy", "at": when(first)[1]}]}, None,
    )
    assert f"\n{WHOLE}: its copy as it was at {when(second)[0]}\n{GONE}: its copy set aside at {when(first)[0]}\n{WHERE}" in said(gone)
    # Its row says what its computer keeps set aside of its work, and what went.
    [found] = await rows(api, project)
    assert found["set_aside"] == {
        "kept": [{"kind": "copy", "at": when(second)[1]}], "snapshots": 0, "gone": [{"kind": "copy", "at": when(first)[1]}],
    }
    # A command typed in the thread ends a turn of its own, which says nothing of it: the row still says what is kept,
    # and not what an older failure recorded.
    await woken(api, monkeypatch, thread, said="/goal status")
    *_, command = await api.app.state.session_store.get_events(thread.id, types=[EventType.SESSION_COMPLETE])
    assert "answers" in command.data and "set_aside" not in command.data
    assert [row["set_aside"] for row in await rows(api, project)] == [found["set_aside"]]


async def test_what_a_landing_set_aside_of_its_copy_reaches_the_threads_report_once_and_stands_on_its_row(api, computer, monkeypatch):
    project, master, thread = await begun_with_copy(api, computer)
    copy = computer.app.places.copy(str(thread.id))

    def meanwhile(frame, outcome):
        if frame["invocation_id"] == "land:0" and frame["args"].get("action") == "revisions":
            # You save your report after the landing's look: its pickup takes it, and its record gives the copy your file.
            (computer.folder / "Report.docx").write_bytes(b"PK report v2, by you")
        if frame["invocation_id"] == "land:0" and frame["args"].get("action") == "commit":
            # A command still running writes the copy after its turn was committed: the record sets that aside.
            (copy / "late.md").write_text("written after the turn was committed\n")
        return outcome

    computer.app.lie = meanwhile
    await woken(api, monkeypatch, thread, [NOTES, _final_response("Noted.")])
    computer.app.lie = None
    assert (computer.folder / "Notes.md").read_text() == "notes\n" and (copy / "Report.docx").read_bytes() == b"PK report v2, by you"
    assert not (copy / "late.md").exists() and not (computer.folder / "late.md").exists()
    # The record set it aside: a snapshot in the thread's repository, which holds the file as the command wrote it.
    [row] = await records(api, thread)
    [aside] = [s["result"]["set_aside"] for s in row.steps if s["tool_name"] == "history.record"]
    shown = subprocess.run(
        ["git", "--git-dir", str(computer.app.places.place / "clones" / str(thread.id)), "show", f"{aside}:late.md"],
        capture_output=True, check=True,
    )
    assert shown.stdout == b"written after the turn was committed\n"
    # The turn's report says so, once: the thread's next open names it, and its report says nothing of it.
    await woken(api, monkeypatch, thread, [_final_response("Done.")], said="Go on.")
    first, second = await told(api, master)
    assert (first.data["set_aside"], second.data.get("set_aside")) == ({"kept": [], "snapshots": 1, "gone": []}, None)
    # Only the thread's own end keeps the snapshot's id, for the next end to read.
    *_, end = await api.app.state.session_store.get_events(thread.id, types=[EventType.SESSION_COMPLETE])
    assert end.data["set_aside"]["snapshots"] == [aside] and aside not in json.dumps([first.data, second.data])
    assert f"\n{SNAPSHOTS}: what the copy held of the thread's own, in 1 snapshot\n{WHERE}" in said(first)
    assert "set aside" not in said(second).lower()
    [found] = await rows(api, project)
    assert found["set_aside"] == {"kept": [], "snapshots": 1, "gone": []}
    # A turn's end that cannot read what was set aside records nothing of it: the row says what the last end that did recorded.
    async def unread(**_):
        raise ConnectionError("the database went away")

    monkeypatch.setattr(completion, "set_aside_of", unread)
    await woken(api, monkeypatch, thread, [_final_response("Done.")], said="Go on.")
    *_, end = await api.app.state.session_store.get_events(thread.id, types=[EventType.SESSION_COMPLETE])
    assert "set_aside" not in end.data
    assert [row["set_aside"] for row in await rows(api, project)] == [found["set_aside"]]


async def test_a_thread_on_a_computer_that_set_nothing_aside_says_nothing_of_it(api, computer, monkeypatch):
    project, master, thread = await begun_with_copy(api, computer)
    await woken(api, monkeypatch, thread, [NOTES, _final_response("Noted.")])
    [report] = await told(api, master)
    assert "set_aside" not in report.data and "set aside" not in said(report).lower()
    [end] = await api.app.state.session_store.get_events(thread.id, types=[EventType.SESSION_COMPLETE])
    assert "set_aside" not in end.data
    [found] = await rows(api, project)
    assert found["set_aside"] is None
