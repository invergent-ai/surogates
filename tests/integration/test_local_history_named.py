"""A landing on the user's computer applies only what its thread did: where nothing else could change its copy, what its own operations named.

Each scene runs a worker's real wake on the tests' computer, as the landing's
own scenes do (``test_local_history_landing``), on its land rules and on the
app's own file helper in their place, and reads the folder back.  A guest
that is not ours stands in for a copy that is not whole: as the landing
begins, it takes files out of the thread's copy, or cuts them short.  Where
the thread ran no command, started no process and has no helper, nothing it
did could have, and the journal names every file it wrote or deleted.
"""

from __future__ import annotations

import base64
import os
from uuid import UUID, uuid5

import pytest

from surogates.devices.operations import JournalRunner, OperationRequest
from surogates.harness import local_landing
from surogates.harness.local_landing import land_local_turn
from surogates.session.events import EventType
from surogates.session.provisioning import create_child_session
from tests.test_fake_places import built_helper  # noqa: F401  (a fixture)
from tests.test_steer_loop import _final_response

from .test_devices import FOLDER, api, link_url  # noqa: F401  (api and link_url are fixtures)
from .test_local_history_landing import files_of, here, holding, kept, lie_at, ran, records, said, seen, told  # noqa: F401  (here is a fixture)
from .test_local_history_open import begun_with_copy, turn_end, woken
from .test_local_history_threads import blob, computer, connected_thread, tool  # noqa: F401  (computer is a fixture)
from .test_local_threads import journal
from .test_turn_sagas import calling
from .test_workstream_threads import events_of

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: What a guest that is not ours says the copy changed, as the landing begins: the thread's one file and two of yours.
SAYS = {"ok": {"paths": ["Budget.xlsx", "Plan.docx", "Report.docx"]}}
#: A turn that wrote one file with a file tool, and ran nothing.
ONE_FILE = calling(("write_file", {"path": "Budget.xlsx", "content": "Total,42\n"}))


def but_the_budget(folder) -> dict[str, tuple]:
    return {path: entry for path, entry in seen(folder).items() if path != "Budget.xlsx"}


# -- a copy that is not whole --------------------------------------------------------------------------------------


async def test_a_copy_short_of_files_its_thread_never_touched_deletes_none_of_them_from_the_folder(api, here, monkeypatch):
    (here.folder / "Plan.docx").write_bytes(b"PK plan v1")
    _, master, thread = await begun_with_copy(api, here)
    copy = here.app.places.copy(str(thread.id))
    before = seen(here.folder)

    def half_a_copy(frame, outcome):
        # As the landing begins, its copy holds some of the folder's files and no more.
        (copy / "Report.docx").unlink()
        (copy / "Plan.docx").unlink()
        return SAYS

    lie_at(here, "changed", half_a_copy)
    await woken(api, monkeypatch, thread, [ONE_FILE, _final_response("Done.")])
    # Your files are in the folder, entry for entry, as they were; the thread's one file landed.
    assert but_the_budget(here.folder) == before and (here.folder / "Budget.xlsx").read_text() == "Total,42\n"
    assert ran(here, "land:0") == ["changed", "revisions", "pickup", "commit", "apply", "record", "forget"]
    [report] = await told(api, master)
    assert files_of(report) == [
        ("Budget.xlsx", "landed", None), ("Plan.docx", "not_merged", "gone"), ("Report.docx", "not_merged", "gone"),
    ]
    assert "the thread's copy lacks them, and none of the thread's file tools deleted them" in said(report)
    # The landing gave the copy the folder's files back; nothing is redone, and nothing waits on you.
    assert (copy / "Report.docx").read_bytes() == b"PK report v1" and (copy / "Plan.docx").read_bytes() == b"PK plan v1"
    assert await events_of(api, thread.id, EventType.HISTORY_REDO, EventType.INBOX_ACTION_REQUIRED) == []
    # Neither is the thread's work, or a file of its in the folder's records: its row holds what it landed.
    [end] = await events_of(api, thread.id, EventType.SESSION_COMPLETE)
    assert end.data["saved"] is True
    [row] = await records(api, thread)
    assert [(f["path"], f["merged"]) for f in row.files] == [("Budget.xlsx", True)]
    # Nor does the folder's history name them as the thread's, left out of its landing.
    [record] = [s for s in row.steps if s["tool_name"] == "history.record"]
    assert [value for key, value in record["arguments"]["trailers"] if key == "Surogate-Not-Merged"] == []
    assert kept(here) == {} and here.app.places.holder is None


async def test_a_copy_whose_files_are_cut_short_lands_none_of_them_over_yours(api, here, monkeypatch):
    (here.folder / "Plan.docx").write_bytes(b"PK plan v1, your own words, all of them")
    _, master, thread = await begun_with_copy(api, here)
    copy = here.app.places.copy(str(thread.id))
    before = seen(here.folder)

    def cut_short(frame, outcome):
        # As the landing begins, two files of its copy are cut short.
        (copy / "Report.docx").write_bytes(b"")
        (copy / "Plan.docx").write_bytes(b"PK plan")
        return SAYS

    lie_at(here, "changed", cut_short)
    await woken(api, monkeypatch, thread, [ONE_FILE, _final_response("Done.")])
    assert but_the_budget(here.folder) == before and (here.folder / "Budget.xlsx").read_text() == "Total,42\n"
    assert (here.folder / "Report.docx").read_bytes() == b"PK report v1"
    assert ran(here, "land:0") == ["changed", "revisions", "pickup", "commit", "apply", "record", "forget"]
    [report] = await told(api, master)
    assert files_of(report) == [
        ("Budget.xlsx", "landed", None), ("Plan.docx", "not_merged", "unnamed"), ("Report.docx", "not_merged", "unnamed"),
    ]
    assert "none of the thread's file tools wrote them and it ran no command" in said(report)
    # The copy has the folder's files again; nothing is redone, nothing waits on you, and the turn's work is saved.
    assert (copy / "Report.docx").read_bytes() == b"PK report v1"
    assert (copy / "Plan.docx").read_bytes() == b"PK plan v1, your own words, all of them"
    assert await events_of(api, thread.id, EventType.HISTORY_REDO, EventType.INBOX_ACTION_REQUIRED) == []
    [end] = await events_of(api, thread.id, EventType.SESSION_COMPLETE)
    assert end.data["saved"] is True
    [row] = await records(api, thread)
    assert [(f["path"], f["merged"]) for f in row.files] == [("Budget.xlsx", True)]


@pytest.mark.slow
async def test_a_file_nothing_the_thread_did_names_is_none_of_its_own_though_the_folder_has_a_second_name_for_it(api, here, monkeypatch):
    (here.folder / "Twin.txt").write_text("one\n")
    other_name = here.folder.parent / "its-other-name.txt"
    other_name.hardlink_to(here.folder / "Twin.txt")
    _, master, thread = await begun_with_copy(api, here)
    copy = here.app.places.copy(str(thread.id))
    before = seen(here.folder)
    lie_at(here, "changed", lambda frame, outcome: (copy / "Twin.txt").write_text("on") and {"ok": {"paths": ["Budget.xlsx", "Twin.txt"]}})
    await woken(api, monkeypatch, thread, [ONE_FILE, _final_response("Done.")])
    assert but_the_budget(here.folder) == before
    [report] = await told(api, master)
    assert files_of(report) == [("Budget.xlsx", "landed", None), ("Twin.txt", "not_merged", "unnamed")]
    # Not left in the copy as the thread's, which no landing writes: the copy has the folder's file again.
    assert (copy / "Twin.txt").read_text() == "one\n"
    # So once the folder's second name is gone, the thread's next landing has nothing of it to land.
    other_name.unlink()
    await woken(api, monkeypatch, thread, [
        calling(("write_file", {"path": "Other.md", "content": "other\n"})), _final_response("Done."),
    ], said="And the other one.")
    _, report = await told(api, master)
    assert files_of(report) == [("Other.md", "landed", None)] and (here.folder / "Twin.txt").read_text() == "one\n"


# -- a link -------------------------------------------------------------------------------------------------------


async def test_a_write_through_a_link_in_the_folder_lands_what_the_copy_holds_whichever_worker_lands_it(api, here, monkeypatch):
    (here.folder / "docs").mkdir()
    (here.folder / "docs" / "README.md").write_text("one\n")
    (here.folder / "README.md").symlink_to("docs/README.md")
    _, master, thread = await begun_with_copy(api, here)
    copy = here.app.places.copy(str(thread.id))
    # Its computer follows the link, and writes docs/README.md: a link the server cannot tell from one a guest made,
    # so this write names no file, as a command does, and the landing lands what the copy holds.
    await woken(api, monkeypatch, thread, [
        calling(("patch", {"mode": "replace", "path": "README.md", "old_string": "one", "new_string": "one, and two"})),
        _final_response("Done."),
    ])
    [report] = await told(api, master)
    assert files_of(report) == [("docs/README.md", "landed", None)]
    for where in (here.folder, copy):
        assert (where / "docs" / "README.md").read_text() == "one, and two\n", where
        assert os.readlink(where / "README.md") == "docs/README.md", where
    assert await journal(api).named(thread.id, FOLDER, besides="land:0") is None
    assert await events_of(api, thread.id, EventType.HISTORY_REDO, EventType.INBOX_ACTION_REQUIRED) == []


async def test_a_link_a_guest_makes_in_the_copy_has_the_landing_land_what_the_copy_holds_as_a_commands_does(api, here, monkeypatch):
    (here.folder / "Budget.csv").write_text("your budget, all of it\n")
    (here.folder / "Notes.md").write_text("notes v1\n")
    _, master, thread = await begun_with_copy(api, here)
    copy = here.app.places.copy(str(thread.id))

    async def a_link(harness) -> None:
        # Once the turn's copy is open: a guest that is not ours makes the copy's Notes.md a link to Budget.csv.
        (copy / "Notes.md").unlink()
        (copy / "Notes.md").symlink_to("Budget.csv")

    def put_back(frame, outcome):
        # And puts Notes.md back as the folder has it once the landing has asked what changed.
        (copy / "Notes.md").unlink()
        (copy / "Notes.md").write_text("notes v1\n")

    lie_at(here, "changed", put_back)
    await woken(api, monkeypatch, thread, [
        calling(("memory", {"action": "add", "content": "Noted."})),
        calling(("read_file", {"path": "Notes.md"})),
        calling(("write_file", {"path": "Notes.md", "content": "the thread's notes\n"})),
        _final_response("Done."),
    ], during=a_link)
    # The thread's write went through the link, and its computer said so: the server cannot tell this link from yours,
    # so the write names no file, and the landing lands what the copy holds.  Budget.csv is never landed as a file
    # the thread named; it lands as a command's work would, its version from before a version in the folder's history.
    [report] = await told(api, master)
    assert files_of(report) == [("Budget.csv", "landed", None)]
    assert (here.folder / "Budget.csv").read_text() == "the thread's notes\n" and (here.folder / "Notes.md").read_text() == "notes v1\n"
    assert (copy / "Budget.csv").read_text() == "the thread's notes\n" and (copy / "Notes.md").read_text() == "notes v1\n"
    [row] = await records(api, thread)
    assert [(f["path"], f["before"], f["merged"]) for f in row.files] == [("Budget.csv", blob(b"your budget, all of it\n"), True)]
    assert await journal(api).named(thread.id, FOLDER, besides="land:0") is None


# -- what names a file, and what names none ------------------------------------------------------------------------


@pytest.mark.slow
async def test_every_file_a_threads_file_tools_named_lands_and_a_command_lands_whatever_its_copy_holds(api, here, monkeypatch):
    (here.folder / "old.md").write_text("old.md v1\n")
    _, master, thread = await begun_with_copy(api, here)
    copy = here.app.places.copy(str(thread.id))
    # Ten files by a file tool, one of them in a folder of its own and one written again, and a file deleted by one.
    await woken(api, monkeypatch, thread, [
        calling(*(("write_file", {"path": f"notes/{n}.md" if n else "Budget.xlsx", "content": f"{n}\n"}) for n in range(10))),
        calling(("patch", {"mode": "replace", "path": "Budget.xlsx", "old_string": "0", "new_string": "Total,42"})),
        calling(("patch", {"mode": "patch", "patch": "*** Begin Patch\n*** Delete File: old.md\n\n*** End Patch"})),
        _final_response("Done."),
    ])
    [report] = await told(api, master)
    assert files_of(report) == [
        ("Budget.xlsx", "landed", None), *((f"notes/{n}.md", "landed", None) for n in range(1, 10)), ("old.md", "landed", None),
    ]
    assert (here.folder / "Budget.xlsx").read_text() == "Total,42\n" and not (here.folder / "old.md").exists()
    assert [(here.folder / "notes" / f"{n}.md").read_text() for n in range(1, 10)] == [f"{n}\n" for n in range(1, 10)]
    # Behind its landing, nothing is named: the next turn's window begins there.
    assert await journal(api).named(thread.id, FOLDER) == frozenset()

    def beside(frame, outcome):
        # What its command started wrote a file of its own in the copy, which no tool names.
        (copy / "Beside.md").write_text("beside\n")
        return {"ok": {"paths": ["Beside.md", "Made.md"]}}

    lie_at(here, "changed", beside, invocation=f"land:{await turn_end(api, thread)}")
    # A turn that ran a command lands what its copy holds: a command changes any file, and the server cannot tell which.
    await woken(api, monkeypatch, thread, [calling(("terminal", {"command": "echo made > Made.md"})), _final_response("Done.")], said="More.")
    _, report = await told(api, master)
    assert files_of(report) == [("Beside.md", "landed", None), ("Made.md", "landed", None)]
    assert (here.folder / "Beside.md").read_text() == "beside\n" and (here.folder / "Made.md").read_text() == "made\n"


async def test_a_thread_that_really_deletes_a_folder_lands_its_deletions(api, here, monkeypatch):
    (here.folder / "old").mkdir()
    for name in ("old/a.md", "old/b.md"):
        (here.folder / name).write_text("x\n")
    _, master, thread = await begun_with_copy(api, here)
    await woken(api, monkeypatch, thread, [
        calling(("terminal", {"command": "rm -r old && echo Total,42 > Budget.xlsx"})), _final_response("Done."),
    ])
    # Both deletions land, and the folder they emptied goes with them.
    assert sorted(p.name for p in here.folder.iterdir()) == ["Budget.xlsx", "Plans", "Report.docx"]
    [report] = await told(api, master)
    assert files_of(report) == [("Budget.xlsx", "landed", None), ("old/a.md", "landed", None), ("old/b.md", "landed", None)]
    # After that landing, a turn that runs nothing names nothing: the command is behind the landing it landed with.
    ops = journal(api)
    assert await ops.named(thread.id, FOLDER) == frozenset()
    # What the thread's helper changes, in the same copy, is the thread's to land: no list of names holds it.
    await create_child_session(store=api.app.state.session_store, parent=thread, channel="worker")
    assert await ops.named(thread.id, FOLDER) is None


@pytest.mark.slow
async def test_a_later_turns_deletion_lands_though_its_copy_holds_what_an_earlier_turn_left_out(api, here, monkeypatch):
    (here.folder / "old.md").write_text("old.md v1\n")
    _, master, thread = await begun_with_copy(api, here)
    # Its first turn runs a script, as any coding thread does: python leaves __pycache__ in the copy, which outlives the turn.
    await woken(api, monkeypatch, thread, [
        calling(("terminal", {"command": "mkdir -p __pycache__ && echo x > __pycache__/tool.cpython-312.pyc && echo Total,42 > Budget.xlsx"})),
        _final_response("Done."),
    ])
    await woken(api, monkeypatch, thread, [
        calling(("terminal", {"command": "rm old.md && echo Total,43 > Budget.xlsx"})), _final_response("Removed the old notes."),
    ], said="Remove the old notes.")
    first, second = await told(api, master)
    # The second turn wrote nothing history leaves out, so its deletion is no move git could not see: it lands.
    assert files_of(second) == [("Budget.xlsx", "landed", None), ("old.md", "landed", None)]
    assert not (here.folder / "old.md").exists() and (here.folder / "Budget.xlsx").read_text() == "Total,43\n"
    # And what the first turn left out is said once, by the turn that made it.
    assert first.data["excluded"] == ["__pycache__/"] and "excluded" not in second.data


@pytest.mark.slow
async def test_a_file_an_earlier_landing_left_in_the_copy_is_still_its_threads_own_and_lands_once_it_can(api, here, monkeypatch):
    (here.folder / "Twin.txt").write_text("one\n")
    other_name = here.folder.parent / "its-other-name.txt"
    other_name.hardlink_to(here.folder / "Twin.txt")
    _, master, thread = await begun_with_copy(api, here)
    await woken(api, monkeypatch, thread, [
        calling(("patch", {"mode": "replace", "path": "Twin.txt", "old_string": "one", "new_string": "one, and two"})),
        _final_response("Done."),
    ])
    # A file with a second name is never replaced: the thread's version stays in its copy.
    [report] = await told(api, master)
    assert files_of(report) == [("Twin.txt", "not_merged", "linked")] and (here.folder / "Twin.txt").read_text() == "one\n"
    # You take its other name away.  The thread's next turn writes another file, and names Twin.txt with nothing:
    # it is the thread's own all the same, which the last landing left in the copy, and it lands now.
    other_name.unlink()
    await woken(api, monkeypatch, thread, [
        calling(("write_file", {"path": "Other.md", "content": "other\n"})), _final_response("Done."),
    ], said="And the other one.")
    _, report = await told(api, master)
    assert files_of(report) == [("Other.md", "landed", None), ("Twin.txt", "landed", None)]
    assert (here.folder / "Twin.txt").read_text() == "one, and two\n"


async def test_a_write_its_computer_has_not_answered_yet_names_its_file(api, computer, monkeypatch):
    _, master, thread = await begun_with_copy(api, computer)
    await woken(api, monkeypatch, thread, [ONE_FILE, _final_response("Done.")])
    ops = journal(api)
    asked = {"device_id": UUID(computer.device_id), "root_session_id": thread.id, "calling_session_id": thread.id}
    key = await JournalRunner(ops, **asked, invocation_id="request:later").run("resolve", {"path": "Later.md"})
    # Asked, and not run yet: it may run whenever its computer takes it, and its file is the thread's.
    await ops._record(OperationRequest(
        **asked, invocation_id="request:later", ordinal=2, kind="write", args={"key": key["ok"], "data": base64.b64encode(b"later\n").decode()},
    ))
    assert await ops.named(thread.id, FOLDER) == {"later.md"}


@pytest.mark.parametrize("what", ["a command", "a write"])
async def test_what_was_asked_before_a_landing_and_ended_after_it_began_is_counted_after_it(api, computer, monkeypatch, what):
    kind = "run" if what == "a command" else "write"
    _, master, thread = await begun_with_copy(api, computer)
    ops = journal(api)
    asked = {"device_id": UUID(computer.device_id), "root_session_id": thread.id, "calling_session_id": thread.id}
    # Asked before the thread's landing, and taken by its computer, which has not answered it: it may still run, so it
    # is the next landing's to count.
    held = holding(computer, lambda frame: frame["invocation_id"] == "request:early" and frame["kind"] in ("run", "write"))
    if what == "a command":
        await ops._record(OperationRequest(
            **asked, invocation_id="request:early", ordinal=1, kind="run", args={"command": "true", "workdir": None, "timeout": 60},
        ))
    else:
        key = await JournalRunner(ops, **asked, invocation_id="request:early").run("resolve", {"path": "Early.md"})
        await ops._record(OperationRequest(
            **asked, invocation_id="request:early", ordinal=2, kind="write", args={"key": key["ok"], "data": base64.b64encode(b"early\n").decode()},
        ))
    await woken(api, monkeypatch, thread, [ONE_FILE, _final_response("Done.")])
    assert [row.saga_state for row in await records(api, thread)] == ["completed"] and [f["kind"] for f in held] == [kind]
    # It ends after that landing began: inside the next landing's window, though it was asked before it.
    assert await ops.cancel_invocation(thread.id, "request:early") == 1
    assert await ops.named(thread.id, FOLDER) == (None if what == "a command" else {"early.md"})


@pytest.mark.parametrize("what", ["a process it started", "a command not answered yet", "a write it resolved nothing for"])
async def test_a_thread_that_could_have_changed_a_file_no_operation_of_its_names_has_no_list(api, computer, monkeypatch, what):
    _, master, thread = await begun_with_copy(api, computer)
    ops = journal(api)
    if what == "a process it started":
        # A background process may still run, and write, long after its turn and its landing.
        await woken(api, monkeypatch, thread, [
            calling(("terminal", {"command": "echo started", "background": True})), _final_response("Started."),
        ])
        assert await ops.named(thread.id, FOLDER) is None
        return
    await woken(api, monkeypatch, thread, [ONE_FILE, _final_response("Done.")])
    assert await ops.named(thread.id, FOLDER) == frozenset()
    asked = {"device_id": UUID(computer.device_id), "root_session_id": thread.id, "calling_session_id": thread.id}
    if what == "a command not answered yet":
        # Asked since that landing, and not run yet: it may run, and change any file, whenever its computer takes it.
        await ops._record(OperationRequest(
            **asked, invocation_id="request:waits", ordinal=1, kind="run", args={"command": "echo later > Later.md", "workdir": None, "timeout": 60},
        ))
        assert await ops.named(thread.id, FOLDER) is None
        return
    # A write whose key no resolve of its own call answered: the server cannot say which file it wrote.
    runner = JournalRunner(ops, **asked, invocation_id="request:odd")
    await runner.run("write", {"key": f"{FOLDER}/Odd.md", "data": base64.b64encode(b"odd\n").decode()})
    assert await ops.named(thread.id, FOLDER) is None


async def test_a_record_its_computer_answered_with_no_commit_is_no_landing_that_names_begin_from(api, computer):
    _, thread = await connected_thread(api, computer)
    assert (await tool(api, thread, "write_file", path="Budget.xlsx", content="Total,42\n"))["status"] == "ok"
    # A record of the thread's, asked under a landing's name, that its computer answered with what is no commit.
    lie_at(computer, "record", lambda frame, outcome: {"ok": {"commit": "not a commit", "set_aside": None}}, invocation="land:9")
    runner = JournalRunner(
        journal(api), device_id=UUID(computer.device_id), root_session_id=thread.id, calling_session_id=thread.id, invocation_id="land:9",
    )
    assert (await runner.run("history", {"action": "record"}))["ok"]["commit"] == "not a commit"
    # It moved nothing the server knows of: the thread's write before it is its own still.
    assert await journal(api).named(thread.id, FOLDER) == {"budget.xlsx"}


# -- a turn's end taken up again -----------------------------------------------------------------------------------


@pytest.mark.parametrize("where", ["after its record, its row not yet written", "after its row was written, before its forgetting"])
async def test_a_turns_end_taken_up_again_leaves_out_what_its_first_run_left_out_and_tells_it_so(api, computer, monkeypatch, where):
    _, master, thread = await begun_with_copy(api, computer)
    store = api.app.state.session_store
    copy = computer.app.places.copy(str(thread.id))
    assert (await tool(api, thread, "write_file", path="Budget.xlsx", content="Total,42\n"))["status"] == "ok"
    lie_at(computer, "changed", lambda frame, outcome: (copy / "Report.docx").write_bytes(b"") or {"ok": {"paths": ["Budget.xlsx", "Report.docx"]}})
    lease = await store.try_acquire_lease(thread.id, "worker-local", ttl_seconds=60)

    async def lands() -> dict:
        return await land_local_turn(
            store=store, session_factory=api.app.state.session_factory, redis=api.app.state.redis,
            session=await store.get_session(thread.id), lease_token=str(lease.lease_token), saga_settings=None, tool_saga_id=None,
        )

    class Lost(BaseException):
        """A worker lost: nothing of its turn runs on, not even what cleans up after an error."""

    def lost(*args, **kwargs):
        raise Lost

    with monkeypatch.context() as cut:
        cut.setattr(local_landing, "_row_files" if where.startswith("after its record") else "_forgotten", lost)
        with pytest.raises(Lost):
            await lands()
    before = len(computer.app.places.asked)
    again = await lands()
    # Taken up again, it asks its computer nothing it asked already: no settle, and no apply named otherwise.
    gives_back = [("land:0", "forget")] if where.startswith("after its record") else [("land:0:release", "forget")]
    assert ran(computer)[before:] == gives_back
    [row] = await records(api, thread)
    assert (row.saga_state, row.saga_id) == ("completed", f"saga:{uuid5(thread.id, 'land:0')}")
    # And it tells what its first run would have: the file it landed, and the one nothing of the thread's wrote.
    assert again["state"] == "completed" and again["saved"] is True
    assert [(f["ref"], f["landing"], f.get("reason")) for f in again["files"]] == [
        ("Budget.xlsx", "landed", None), ("Report.docx", "not_merged", "unnamed"),
    ]
    assert (computer.folder / "Report.docx").read_bytes() == b"PK report v1"
    await store.release_lease(thread.id, lease.lease_token)
