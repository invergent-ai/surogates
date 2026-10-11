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

import pytest

from surogates.sandbox import local_history
from surogates.session.events import EventType
from tests.test_steer_loop import _final_response

from .test_devices import add_user, api, link_url  # noqa: F401  (api and link_url are fixtures)
from .test_file_history import history_of, opened, restored, undone
from .test_local_history_landing import EDITS, lie_at, records, said, seen, told
from .test_local_history_open import begun_with_copy, woken
from .test_local_history_threads import computer  # noqa: F401  (a fixture)
from .test_turn_sagas import calling
from .test_workstream_overview import rows

pytestmark = pytest.mark.asyncio(loop_scope="session")


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
    # The folder now holds a file whose name its history cannot keep: the thread's next open finds it has none.
    computer.app.places.off = "names"
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
