"""A project's files in its threads' rows: what each thread's landings changed, how each file
stands, and the stream's word of a landing another lock holder finished.  And a file's History in
the Library: its versions read from the project's records, each still kept or not; Open version,
a version's bytes as data to save; and the files that are gone, listed so their History is reached."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
from pathlib import Path
from urllib.parse import quote, unquote
from uuid import UUID

import pytest
from sqlalchemy import text

from surogates.api.routes import workstreams as routes_module
from surogates.harness import landing as landing_module
from surogates.sandbox.history import HistoryError
from surogates.session.store import SessionStore
from surogates.storage.tenant import boundary_workspace_prefix
from surogates.workstreams import bucket as bucket_module
from surogates.workstreams import history as rows_module
from surogates.workstreams import stream as project_stream
from surogates.workstreams.bucket import BucketHistory, Busy
from surogates.workstreams.derive import SHELL_LIMITS, utc
from surogates.workstreams.store import WorkstreamStore
from surogates.sandbox.pool import SandboxPool, sandbox_session_key
from surogates.session.events import EventType
from tests.test_durable_history import cut_history
from tests.test_steer_loop import _final_response

from .test_devices import add_user, api  # noqa: F401  (api is a fixture)
from .test_durable_landings import (  # noqa: F401  (a_short_fence is a fixture)
    a_landing_killed,
    a_short_fence,
    edited,
    ends,
    rows,
    rows_stand,
    stored,
)
from .test_redo_loop import a_clash, a_routine_run, in_its_pod, pickups_of
from .test_thread_copies import a_thread, git, open_pod, pods, reports  # noqa: F401  (pods is a fixture)
from .test_thread_helpers import a_coordinating_thread, helpers_of
from .test_turn_sagas import a_turn, calling, stop
from .test_workstream_overview import act_on
from .test_workstream_overview import rows as thread_rows
from .test_workstream_overview import streamed
from .test_workstream_threads import call_tool
from .test_workstreams import create, master_of

pytestmark = pytest.mark.asyncio(loop_scope="session")

REQUEST = EventType.LLM_REQUEST


@pytest.fixture(autouse=True)
def copies(api, tmp_path, monkeypatch):
    """The api's copies of the projects' histories in the test's own folder, never the machine's temp folder."""
    monkeypatch.setattr(api.app.state.settings.history, "copies_path", str(tmp_path / "copies"))


async def history_of(api, project: dict, path: str, token: str | None = None, status: int = 200, **more: str):
    response = await api.client.get(
        f"/v1/workstreams/{project['id']}/history", params={"path": path, **more}, headers=api.auth(token),
    )
    assert response.status_code == status, response.text
    return response.json()


async def opened(api, project: dict, version: str, path: str | None, token: str | None = None):
    """Open version: the response to asking for *path* as *version* left it."""
    return await api.client.get(
        f"/v1/workstreams/{project['id']}/history/{quote(version, safe='')}/file",
        params={} if path is None else {"path": path}, headers=api.auth(token),
    )


async def deleted_of(api, project: dict, token: str | None = None, status: int = 200):
    response = await api.client.get(f"/v1/workstreams/{project['id']}/history/deleted", headers=api.auth(token))
    assert response.status_code == status, response.text
    return response.json()


async def copy_of(api, project: dict) -> BucketHistory:
    """The api's copy of *project*'s history."""
    return BucketHistory.of(api.app.state.storage, await master_of(api, project), api.app.state.settings.history)


def written_out(place: BucketHistory) -> list[Path]:
    """The versions the copy holds on their way to who asked for them."""
    return sorted((place.clone / "out").glob("*"))


async def recorded(api, like, *, saga: str, files: list[dict] | None = None, picked_up: list[dict] | None = None, **columns: str) -> int:
    """A record made by hand as the landing *like* was recorded, with these *files*, pickup and *columns*; its id.

    A column is given as SQL: ``saga_state="'running'"``, ``device_id="gen_random_uuid()"``.
    """
    given = {"saga_state": "'completed'", "device_id": "NULL", "workstream_id": "workstream_id", **columns}
    async with api.app.state.session_factory() as db:
        made = (await db.execute(text(
            "INSERT INTO workstream_history "
            "(workstream_id, device_id, kind, saga_id, saga_state, thread_id, agent_id, steps, files, picked_up) "
            f"SELECT {given['workstream_id']}, {given['device_id']}, kind, :saga, {given['saga_state']}, thread_id, agent_id, steps, "
            "cast(:files AS jsonb), cast(:picked_up AS jsonb) FROM workstream_history WHERE id = :id RETURNING id"
        ), {"saga": f"{saga}:{like.id}", "id": like.id, "files": json.dumps(files or []), "picked_up": json.dumps(picked_up or [])})).scalar_one()
        await db.commit()
    return made


async def two_threads(api, tmp_path):
    """A project whose threads A and B each changed its report in turn, over pods on its stored files."""
    project = await create(api)
    master = await master_of(api, project)
    first, second = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master)
    pods = stored(api, first, tmp_path)
    pool = SandboxPool(pods)
    await edited(pool, first, "printf ' by A' >> Report.docx")
    await ends(api, pool, first)
    return project, first, second, pods, pool


async def a_landing(api, tmp_path, command: str):
    """A project whose thread A landed what *command* made of its files, and that landing's row."""
    project = await create(api)
    thread = await a_thread(api, "Draft A", await master_of(api, project))
    pods = stored(api, thread, tmp_path)
    pool = SandboxPool(pods)
    await edited(pool, thread, command)
    await ends(api, pool, thread)
    [row] = await rows(api, thread)
    return project, thread, pods, pool, row


async def marks_of(api, project: dict, thread) -> list[tuple[str, str | None]]:
    """Each file of *thread*'s row, with its mark."""
    [found] = await thread_rows(api, project, thread_id=str(thread.id))
    return [(f["ref"], f["landing"]) for f in found["files"]]


async def test_a_threads_row_lists_what_its_landing_changed_each_as_it_landed(api, tmp_path):
    project, thread, pods, pool, row = await a_landing(
        api, tmp_path, "printf ' by A' >> Report.docx && echo a > a.md && rm notes.txt",
    )
    # A deletion is no file to open: the row lists what is there.
    assert await marks_of(api, project, thread) == [("Report.docx", "landed"), ("a.md", "landed")]
    [listed] = [found for found in await thread_rows(api, project) if found["id"] == str(thread.id)]
    assert listed["files"][0] == {
        "kind": "file", "label": "Report.docx", "ref": "Report.docx", "thread_id": str(thread.id), "landing": "landed",
    }


async def test_a_file_your_edit_clashed_with_is_being_redone_until_the_redo_lands_it(api, monkeypatch, pods):
    project = await create(api)
    thread = await a_thread(api, "Draft A", await master_of(api, project))
    pool = SandboxPool(pods)
    await a_clash(api, monkeypatch, pods, pool, thread, b"PK\x03\x04 report v2 by you")
    # What its landing left out comes first.
    assert await marks_of(api, project, thread) == [("Report.docx", "redoing"), ("a.md", "landed")]
    # A read that takes no files, as the coordinator's list of its threads does, reads no landing and no redo.
    [bare] = await WorkstreamStore(api.app.state.session_factory).thread_facts(UUID(project["id"]), with_files=False)
    assert (bare.landings, bare.redoing) == ((), frozenset())
    await a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "printf ' by A' >> Report.docx"})), _final_response("Redid the edit."),
    ], pool=pool)
    assert await marks_of(api, project, thread) == [("Report.docx", "landed"), ("a.md", "landed")]


@pytest.mark.parametrize("since, mark", [
    ((), "redoing"),
    ((REQUEST,), "redoing"),
    ((REQUEST, EventType.SESSION_COMPLETE), "not_merged"),
    ((REQUEST, EventType.SESSION_FAIL), "not_merged"),
    ((REQUEST, EventType.SESSION_PAUSE), "not_merged"),
    ((REQUEST, EventType.SESSION_STOPPED), "not_merged"),
    ((EventType.SESSION_FAIL,), "redoing"),
    ((EventType.SESSION_FAIL, REQUEST, EventType.SESSION_STOPPED), "not_merged"),
], ids=[
    "told", "its turn under way", "its turn ended and left the file", "its turn failed", "its turn stopped",
    "its turn stopped by the route", "a turn refused before it asked the model, which read nothing", "the turn after that one stopped",
])
async def test_a_file_is_being_redone_exactly_while_its_threads_next_turn_would_redo_it(api, monkeypatch, pods, since, mark):
    project = await create(api)
    thread = await a_thread(api, "Draft A", await master_of(api, project))
    store, pool = api.app.state.session_store, SandboxPool(pods)
    await a_clash(api, monkeypatch, pods, pool, thread, b"PK\x03\x04 report v2 by you")
    for kind in since:
        await store.emit_event(thread.id, kind, {})
    assert await marks_of(api, project, thread) == [("Report.docx", mark), ("a.md", "landed")]
    # The landing's own rule, for the turn the thread takes next.
    assert await landing_module.redo_files(store, thread.id) == ({"Report.docx"} if mark == "redoing" else set())


async def test_a_resolved_threads_file_is_not_being_redone_until_it_is_reopened(api, monkeypatch, pods):
    project = await create(api)
    thread = await a_thread(api, "Draft A", await master_of(api, project))
    pool = SandboxPool(pods)
    await a_clash(api, monkeypatch, pods, pool, thread, b"PK\x03\x04 report v2 by you")
    assert (await act_on(api, project, thread, "resolve")).status_code == 200
    assert await marks_of(api, project, thread) == [("Report.docx", "not_merged"), ("a.md", "landed")]
    assert (await act_on(api, project, thread, "reopen")).status_code == 200
    # Its next turn still reads the redo.
    assert await marks_of(api, project, thread) == [("Report.docx", "redoing"), ("a.md", "landed")]


async def test_a_file_that_clashed_again_is_not_merged_and_is_landed_once_its_thread_lands_it(api, monkeypatch, pods):
    project = await create(api)
    master = await master_of(api, project)
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await a_clash(api, monkeypatch, pods, pool, thread, b"PK\x03\x04 report v2 by you")
    await a_clash(api, monkeypatch, pods, pool, thread, b"PK\x03\x04 report v3 by you")
    [found] = await thread_rows(api, project, thread_id=str(thread.id))
    # It waits on you over the file: nothing redoes it.
    assert (found["reason"], [(f["ref"], f["landing"]) for f in found["files"]]) == (
        "files", [("Report.docx", "not_merged"), ("a.md", "landed")],
    )
    await call_tool(api, master, "message_thread", thread_id=str(thread.id), message="Put your change in again.")
    await a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "printf ' by A' >> Report.docx"})), _final_response("Put it in again."),
    ], pool=pool)
    assert await marks_of(api, project, thread) == [("Report.docx", "landed"), ("a.md", "landed")]


async def test_a_thread_with_no_landing_lists_its_turn_summaries_files_unmarked(api, monkeypatch, pods):
    project = await create(api)
    thread = await a_thread(api, "Draft A", await master_of(api, project))
    store = api.app.state.session_store
    named = {"artifacts": [
        {"kind": "file", "label": "notes.md", "ref": "notes.md"}, {"kind": "artifact", "label": "Sales chart", "ref": "art-1"},
    ]}
    await store.emit_event(thread.id, EventType.TURN_SUMMARY, {"recap": "Wrote notes.", **named})
    assert await marks_of(api, project, thread) == [("notes.md", None), ("art-1", None)]
    # A record with no file is no landing of a file: the thread still works on the real ones.
    async with api.app.state.session_factory() as db:
        await db.execute(text(
            "INSERT INTO workstream_history (workstream_id, kind, saga_id, saga_state, thread_id, agent_id) "
            "VALUES (:project, 'landing', 'saga:nothing', 'completed', :thread, :agent)"
        ), {"project": project["id"], "thread": thread.id, "agent": thread.agent_id})
        await db.commit()
    assert await marks_of(api, project, thread) == [("notes.md", None), ("art-1", None)]
    # Once a landing recorded its files, they are the row's: a summary gives its artifacts alone.
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo a > a.md")
    await ends(api, pool, thread)
    assert await marks_of(api, project, thread) == [("a.md", "landed"), ("art-1", None)]


async def test_a_computers_records_are_none_of_the_clouds(api, tmp_path):
    project, thread, pods, pool, row = await a_landing(api, tmp_path, "printf ' by A' >> Report.docx")
    # As a folder's landing on a computer will be recorded: the same table, with its computer and its folder.
    async with api.app.state.session_factory() as db:
        await db.execute(text(
            "UPDATE workstream_history SET device_id = gen_random_uuid(), folder = '/home/you/Reports' WHERE id = :id"
        ), {"id": row.id})
        await db.commit()
    assert await marks_of(api, project, thread) == []


async def test_only_a_threads_own_completed_landings_in_its_project_mark_its_files(api, tmp_path):
    project, thread, pods, pool, row = await a_landing(api, tmp_path, "printf ' by A' >> Report.docx")
    other = await create(api)
    async with api.app.state.session_factory() as db:
        # A record of this thread under another project, one still running, and one that is no landing:
        # none is this row's.
        await db.execute(text(
            "INSERT INTO workstream_history (workstream_id, kind, saga_id, saga_state, thread_id, agent_id, files) "
            "SELECT workstream_id, 'pickup', 'saga:pickup', 'completed', thread_id, agent_id, "
            "'[{\"path\": \"picked.md\", \"before\": null, \"after\": \"k1\", \"merged\": true}]'::jsonb "
            "FROM workstream_history WHERE id = :id"
        ), {"id": row.id})
        await db.execute(text(
            "INSERT INTO workstream_history (workstream_id, kind, saga_id, saga_state, thread_id, agent_id, files) "
            "SELECT :other, 'landing', 'saga:elsewhere', 'completed', thread_id, agent_id, "
            "'[{\"path\": \"elsewhere.md\", \"before\": null, \"after\": \"e1\", \"merged\": true}]'::jsonb "
            "FROM workstream_history WHERE id = :id"
        ), {"other": other["id"], "id": row.id})
        await db.execute(text(
            "INSERT INTO workstream_history (workstream_id, kind, saga_id, saga_state, thread_id, agent_id, files) "
            "SELECT workstream_id, 'landing', 'saga:running', 'running', thread_id, agent_id, "
            "'[{\"path\": \"running.md\", \"before\": null, \"after\": \"r1\", \"merged\": true}]'::jsonb "
            "FROM workstream_history WHERE id = :id"
        ), {"id": row.id})
        await db.commit()
    assert await marks_of(api, project, thread) == [("Report.docx", "landed")]


async def a_landing_left_pushed(api, monkeypatch, tmp_path):
    """A project whose thread B's landing pushed and was killed before its row said so, its fence
    long past; and thread A, with a change of its own to land."""
    project = await create(api)
    master = await master_of(api, project)
    first, second = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master)
    pool = SandboxPool(stored(api, first, tmp_path))
    await edited(pool, second, "echo b > b.md")
    await a_landing_killed(api, monkeypatch, pool, second, after="record")
    async with api.app.state.session_factory() as db:
        await db.execute(text("UPDATE workstream_history SET updated_at = now() - interval '1 hour' WHERE saga_state = 'running'"))
        await db.commit()
    await edited(pool, first, "echo c > c.md")
    return project, first, second, pool


def announced(api, monkeypatch, thread) -> list[tuple[str, str, list[str]]]:
    """Each landing the stream is told of: its thread, and that thread's landings' states and
    files as a client refetching at that moment reads them."""
    told: list[tuple[str, str, list[str]]] = []
    publish = landing_module.publish

    async def recorded(redis, workstream_id, session_id, kind):
        told.append((str(session_id), kind, [f"{row.saga_state}: {f['path']}" for row in await rows(api, thread) for f in row.files]))
        await publish(redis, workstream_id, session_id, kind)

    monkeypatch.setattr(landing_module, "publish", recorded)
    return told


async def test_a_threads_landing_another_threads_landing_settles_is_announced_too(api, monkeypatch, tmp_path):
    project, first, second, pool = await a_landing_left_pushed(api, monkeypatch, tmp_path)
    assert await marks_of(api, project, second) == []

    async def act():
        await ends(api, pool, first)

    # A's worker holds the lock next: it completes B's landing, and the stream says B's files changed.
    sent = await streamed(api, monkeypatch, project, 1, act)
    assert sent[:2] == [("ready", {}), ("change", {"thread_id": str(second.id), "type": "history.landed"})]
    # Each thread's row has its own landing's files, in the project's list too.
    listed = {found["title"]: [(f["ref"], f["landing"]) for f in found["files"]] for found in await thread_rows(api, project)}
    assert listed == {"Draft A": [("c.md", "landed")], "Draft B": [("b.md", "landed")]}
    assert await marks_of(api, project, second) == [("b.md", "landed")]


async def test_a_threads_landing_a_routines_pickup_settles_is_announced_too(api, monkeypatch, tmp_path):
    project, first, second, pool = await a_landing_left_pushed(api, monkeypatch, tmp_path)
    run, _ = await a_routine_run(api, await master_of(api, project), "Tidy up")
    await in_its_pod(api, pool, run, "echo tidied > notes.txt")

    async def act():
        await ends(api, pool, run)

    # The routine's run holds the lock next, through the master's pod: it completes B's landing first.
    sent = await streamed(api, monkeypatch, project, 1, act)
    assert sent[:2] == [("ready", {}), ("change", {"thread_id": str(second.id), "type": "history.landed"})]
    assert await marks_of(api, project, second) == [("b.md", "landed")]


async def test_a_routines_own_pickup_adds_nothing_to_any_threads_files(api, tmp_path):
    project, thread, pods, pool, row = await a_landing(api, tmp_path, "printf ' by A' >> Report.docx")
    master = await master_of(api, project)
    before = [(found["id"], found["files"]) for found in await thread_rows(api, project)]
    run, _ = await a_routine_run(api, master, "Tidy up")
    await in_its_pod(api, pool, run, "true")
    # As its calls wrote them.
    (pods.project / "notes.txt").write_text("tidied\n")
    (pods.project / "Report.docx").write_bytes(b"PK\x03\x04 report v1 by A checked")
    await ends(api, pool, run)
    # Its record is the project's, of no thread: the file A landed is still A's landed file, and no row lists the routine's.
    [picked] = await pickups_of(api, master)
    assert (picked.thread_id, sorted(f["path"] for f in picked.picked_up)) == (None, ["Report.docx", "notes.txt"])
    assert [(found["id"], found["files"]) for found in await thread_rows(api, project)] == before
    assert await marks_of(api, project, thread) == [("Report.docx", "landed")]


async def test_a_settled_landing_is_announced_once_and_only_after_its_row_says_it_landed(api, monkeypatch, tmp_path):
    project, first, second, pool = await a_landing_left_pushed(api, monkeypatch, tmp_path)
    told = announced(api, monkeypatch, second)
    await ends(api, pool, first)
    # Whoever hears it reads the row complete, with its files.
    assert told == [(str(second.id), "history.landed", ["completed: b.md"])]
    # A later lock holder finds nothing left running: nothing is said again.
    await edited(pool, first, "echo d > d.md")
    await ends(api, pool, first)
    assert len(told) == 1


def refusing(patch, thread) -> list[int]:
    """The database refuses every write of *thread*'s landing's row as completed, while *patch* lasts: each it refused."""
    save = landing_module.save_landing
    refused: list[int] = []

    async def unwritten(session_factory, row, saga, **values):
        if values.get("state") == "completed" and saga.session_id == thread.id:
            refused.append(row)
            raise ConnectionError("the database went away")
        await save(session_factory, row, saga, **values)

    patch.setattr(landing_module, "save_landing", unwritten)
    return refused


def main_of(pods, *more: str) -> str:
    return git(pods.project / "_history", "rev-parse", "refs/heads/main" + "".join(more))


async def test_a_pushed_landing_whose_row_could_not_be_written_is_landed_over_by_no_one_and_announced_once_it_is(
    api, monkeypatch, tmp_path,
):
    project, first, second, pool = await a_landing_left_pushed(api, monkeypatch, tmp_path)
    pods = pool._backend
    told = announced(api, monkeypatch, second)
    pushed = main_of(pods)
    with monkeypatch.context() as patch:
        refused = refusing(patch, second)
        await ends(api, pool, first)
    # The settle found it pushed, and both tries to write its row so were refused: nothing is
    # announced that a reader would not find, and A's landing does not go over it.  A's turn is
    # kept on its branch, as one whose landing could not start, and main is B's landing still.
    assert len(refused) == 2 and told == []
    assert main_of(pods) == pushed and pods.real_names() == ["Report.docx", "b.md", "notes.txt"]
    assert git(pods.project / "_history", "show", f"refs/heads/threads/{first.id}:c.md") == "c"
    assert await rows(api, first) == [] and [row.saga_state for row in await rows(api, second)] == ["running"]
    [report] = await reports(api, await master_of(api, project))
    assert (report["landing"], report["saved"]) == ("compensated", True)
    assert await marks_of(api, project, second) == []
    # The database answers again: A's next turn settles B's landing, which is told with its files
    # and its commit, and lands its own on it.  B's file was in the project's files throughout.
    await ends(api, SandboxPool(pods), first)
    [landed] = await rows(api, second)
    assert (landed.saga_state, landed.commit, [f["path"] for f in landed.files]) == ("completed", pushed, ["b.md"])
    assert told == [(str(second.id), "history.landed", ["completed: b.md"])]
    assert main_of(pods, "^") == pushed and [row.saga_state for row in await rows(api, first)] == ["completed"]
    assert pods.real_names() == ["Report.docx", "b.md", "c.md", "notes.txt"]
    assert await marks_of(api, project, second) == [("b.md", "landed")]


@pytest.mark.parametrize("pruning", ["goes on regardless too", "settles as it should"])
async def test_a_pushed_landing_another_went_over_is_found_in_mains_history_and_never_put_back(api, monkeypatch, tmp_path, pruning):
    project, first, second, pool = await a_landing_left_pushed(api, monkeypatch, tmp_path)
    pods = pool._backend
    told = announced(api, monkeypatch, second)
    pushed = main_of(pods)
    settle, asked, execute = landing_module.settle_running, [], pods.execute

    async def goes_on_regardless(session_factory, sandbox_pool, *args, **kwargs):
        # A holder that lands over a landing its settle could not write: a lock lost and a fence that fell short leave the same.
        try:
            return await settle(session_factory, sandbox_pool, *args, **kwargs)
        except landing_module.LandingUnsettled:
            if isinstance(sandbox_pool, landing_module._Released) and pruning == "settles as it should":
                raise
            return []

    async def watched(sandbox_id, name, input, **kwargs):
        asked.append(json.loads(input).get("action") if name == "_history" else name)
        return await execute(sandbox_id, name, input, **kwargs)

    monkeypatch.setattr(landing_module, "settle_running", goes_on_regardless)
    monkeypatch.setattr(pods, "execute", watched)
    with monkeypatch.context() as patch:
        refused = refusing(patch, second)
        await ends(api, pool, first)
    # A's landing went over B's, whose row still reads running.  The pruning's settle finds B's
    # landing under A's, puts nothing back, and cannot write the row either: a pruning that settles
    # leaves the day unpruned.
    assert len(refused) == 4 and told == []
    assert main_of(pods, "^") == pushed and pods.real_names() == ["Report.docx", "b.md", "c.md", "notes.txt"]
    assert "unapply" not in asked
    assert ("prune" in asked, (pods.project / "_history" / "pruned").exists()) == ((pruning == "goes on regardless too",) * 2)
    assert [row.saga_state for row in await rows(api, second)] == ["running"]
    # The next holder writes it, with the landing's own commit, not main's.
    await edited(pool, first, "echo d > d.md")
    await ends(api, pool, first)
    [landed] = await rows(api, second)
    assert (landed.saga_state, landed.commit, [f["path"] for f in landed.files]) == ("completed", pushed, ["b.md"])
    assert told == [(str(second.id), "history.landed", ["completed: b.md"])]
    assert "unapply" not in asked and pods.real_names() == ["Report.docx", "b.md", "c.md", "d.md", "notes.txt"]
    assert (pods.project / "b.md").read_text() == "b\n"


async def test_a_landing_that_never_pushed_is_put_back_though_another_went_over_it(api, monkeypatch, tmp_path):
    project = await create(api)
    master = await master_of(api, project)
    first, second = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master)
    pods = stored(api, first, tmp_path)
    pool = SandboxPool(pods)
    await edited(pool, first, "echo s > start.md")
    await ends(api, pool, first)
    began = main_of(pods)
    await edited(pool, second, "echo b > b.md && echo v2 > start.md")
    execute, looks = pods.execute, []

    async def no_push(sandbox_id, name, input, **kwargs):
        if name == "_history" and json.loads(input)["action"] == "record":
            return json.dumps({"commit": None})
        return await execute(sandbox_id, name, input, **kwargs)

    with monkeypatch.context() as patch:  # killed in its record's try, before the push: its row has the step
        patch.setattr(pods, "execute", no_push)
        await a_landing_killed(api, monkeypatch, pool, second, after="record")
    assert main_of(pods) == began
    async with api.app.state.session_factory() as db:
        await db.execute(text("UPDATE workstream_history SET updated_at = now() - interval '1 hour' WHERE saga_state = 'running'"))
        await db.commit()
    told = announced(api, monkeypatch, second)
    await edited(pool, first, "echo c > c.md")

    async def unsettled(*args, **kwargs):
        return []

    with monkeypatch.context() as patch:  # a holder that lost its lock unseen, its fence short: it lands over B's
        patch.setattr(landing_module, "settle_running", unsettled)
        await ends(api, pool, first, pruned=False)
        patch.setattr(landing_module, "prune_after", unsettled)
        await asyncio.gather(*landing_module._PRUNINGS)
    assert (pods.project / "start.md").read_text() == "v2\n" and "b.md" in pods.real_names()
    async def watched(sandbox_id, name, input, **kwargs):
        if name == "_history" and "saga" in (asked := json.loads(input)):
            looks.append((asked["saga"], asked["since"]))
        return await execute(sandbox_id, name, input, **kwargs)

    monkeypatch.setattr(pods, "execute", watched)
    # The next settle looks through main's history back to where B's landing began, and it is not
    # there: its files go back, as before.
    await edited(pool, first, "echo d > d.md")
    await ends(api, pool, first)
    [killed] = await rows(api, second)
    assert (killed.saga_state, killed.files, killed.commit) == ("compensated", [], None) and told == []
    assert looks == [(killed.saga_id, began)]
    assert (pods.project / "start.md").read_text() == "s\n"
    assert pods.real_names() == ["Report.docx", "c.md", "d.md", "notes.txt", "start.md"]


async def test_a_landing_a_prunings_cut_hides_is_given_up_and_none_of_its_files_is_put_back(api, monkeypatch, tmp_path):
    project = await create(api)
    master = await master_of(api, project)
    first, second = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master)
    pods = stored(api, first, tmp_path)
    pool = SandboxPool(pods)
    await edited(pool, first, "echo s > start.md")
    await ends(api, pool, first)
    await edited(pool, second, "echo b > b.md && echo v2 > start.md")
    await a_landing_killed(api, monkeypatch, pool, second, after="record")
    async with api.app.state.session_factory() as db:
        await db.execute(text("UPDATE workstream_history SET updated_at = now() - interval '1 hour' WHERE saga_state = 'running'"))
        await db.commit()
    told = announced(api, monkeypatch, second)
    await edited(pool, first, "echo c > c.md")
    execute, asked = pods.execute, []

    async def unsettled(*args, **kwargs):
        return []

    async def watched(sandbox_id, name, input, **kwargs):
        asked.append(json.loads(input).get("action") if name == "_history" else name)
        return await execute(sandbox_id, name, input, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(landing_module, "settle_running", unsettled)
        await ends(api, pool, first, pruned=False)
        patch.setattr(landing_module, "prune_after", unsettled)
        await asyncio.gather(*landing_module._PRUNINGS)
    # As a pruning cuts it: main's own commits end at A's landing, and B's is behind the cut.
    (pods.project / "_history" / "shallow").write_text(f"{main_of(pods)}\n")
    monkeypatch.setattr(pods, "execute", watched)
    await edited(pool, first, "echo d > d.md")
    await ends(api, pool, first)
    # Whether it pushed is not known, and main may hold its files: none is put back.  It is given
    # up, and its thread waits on you over them.
    [killed] = await rows(api, second)
    assert (killed.saga_state, killed.files, killed.commit) == ("escalated", [], None) and told == []
    assert "unapply" not in asked and (pods.project / "start.md").read_text() == "v2\n" and "b.md" in pods.real_names()
    [report] = [r for r in await reports(api, master) if r.get("recovered")]
    assert (report["landing"], report["gone"], sorted(f["ref"] for f in report["files"])) == ("escalated", True, ["b.md", "start.md"])


async def test_a_routines_pickup_records_nothing_past_a_pushed_landing_whose_row_could_not_be_written(api, monkeypatch, tmp_path):
    project, first, second, pool = await a_landing_left_pushed(api, monkeypatch, tmp_path)
    pods, master = pool._backend, await master_of(api, project)
    pushed = main_of(pods)
    run, _ = await a_routine_run(api, master, "Tidy up")
    await in_its_pod(api, pool, run, "true")
    (pods.project / "notes.txt").write_text("tidied\n")  # as its call wrote it
    with monkeypatch.context() as patch:
        refused = refusing(patch, second)
        await ends(api, pool, run)
    # Nothing is pushed over B's landing, and the run's change stays in the project's files.
    assert len(refused) == 2 and main_of(pods) == pushed and await pickups_of(api, master) == []
    assert (pods.project / "notes.txt").read_text() == "tidied\n"
    # The next landing settles B's, and picks the change up as yours.
    await ends(api, pool, first)
    assert [row.saga_state for row in await rows(api, second)] == ["completed"]
    [landed] = await rows(api, first)
    assert [f["path"] for f in landed.picked_up] == ["notes.txt"]


async def test_a_failed_turns_keep_moves_no_main_past_a_pushed_landing_whose_row_could_not_be_written(api, monkeypatch, tmp_path):
    project, first, second, pool = await a_landing_left_pushed(api, monkeypatch, tmp_path)
    pods = pool._backend
    pushed = main_of(pods)
    with monkeypatch.context() as patch:
        refused = refusing(patch, second)
        await asyncio.wait_for(ends(api, pool, first, failed=True), 60)
    # The keep writes A's own branch alone, once the settle that failed had waited out B's fence.
    assert len(refused) == 2 and main_of(pods) == pushed and pods.real_names() == ["Report.docx", "b.md", "notes.txt"]
    assert git(pods.project / "_history", "show", f"refs/heads/threads/{first.id}:c.md") == "c"
    assert [row.saga_state for row in await rows(api, second)] == ["running"]


async def test_a_settled_landing_whose_thread_is_gone_names_no_thread_on_the_stream(api, monkeypatch, tmp_path):
    project, first, second, pool = await a_landing_left_pushed(api, monkeypatch, tmp_path)
    async with api.app.state.session_factory() as db:  # as deleting its session leaves the record
        await db.execute(text("UPDATE workstream_history SET thread_id = NULL WHERE saga_state = 'running'"))
        await db.commit()
    told = announced(api, monkeypatch, second)
    await ends(api, pool, first)
    async with api.app.state.session_factory() as db:
        states = (await db.execute(text(
            "SELECT saga_state FROM workstream_history WHERE workstream_id = :project AND thread_id IS NULL"
        ), {"project": project["id"]})).scalars().all()
    # No thread's row changed: nothing names one.
    assert states == ["completed"] and told == []


async def test_a_landings_row_knows_the_state_its_own_last_write_left_it_in(monkeypatch):
    written: list[str] = []

    async def saved(session_factory, row, saga, *, state="running", **values):
        if state == "refused":
            raise ConnectionError("the database went away")
        written.append(state)

    monkeypatch.setattr(landing_module, "save_landing", saved)
    row = landing_module._Row(None, 7, None)
    assert row.state is None
    await row.write(state="completed", commit="c1")
    assert (row.state, written) == ("completed", ["completed"])
    # A write that names no outcome writes the row as running again, and one refused changes nothing.
    await row.write()
    assert (row.state, written) == ("running", ["completed", "running"])
    with pytest.raises(ConnectionError):
        await row.write(state="refused")
    assert row.state == "running"


async def test_a_landing_settled_as_put_back_is_not_announced_as_landed(api, monkeypatch, tmp_path):
    project = await create(api)
    master = await master_of(api, project)
    first, second = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master)
    pool = SandboxPool(stored(api, first, tmp_path))
    await edited(pool, second, "echo b > b.md")
    await a_landing_killed(api, monkeypatch, pool, second, after="apply b.md")
    told = announced(api, monkeypatch, second)
    await edited(pool, first, "echo c > c.md")
    await ends(api, pool, first)
    assert [row.saga_state for row in await rows(api, second)] == ["compensated"] and told == []


async def test_the_wait_a_settled_escalation_puts_on_its_thread_reaches_the_projects_stream(api, monkeypatch, tmp_path):
    project = await create(api)
    master = await master_of(api, project)
    first, second = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master)
    pods = stored(api, first, tmp_path)
    pool = SandboxPool(pods)
    # Its row written at every try, as a slow landing's is: a.md's apply is known to have run.
    rows_stand(monkeypatch, "exact")
    await edited(pool, first, "for f in a b c; do echo $f > $f.md; done")
    await a_landing_killed(api, monkeypatch, pool, first, after="apply b.md")
    (pods.project / "a.md").write_text("saved by you over the half landing")  # its put-back finds it changed
    await edited(pool, second, "echo by B > B.md")
    heard: list[tuple[str, str]] = []
    publish = project_stream.publish

    async def recorded(redis, workstream_id, session_id, kind):
        if redis is not None:  # with none, nothing is said
            heard.append((str(session_id), kind))
        await publish(redis, workstream_id, session_id, kind)

    monkeypatch.setattr(project_stream, "publish", recorded)
    monkeypatch.setattr(api.app.state, "session_store", SessionStore(api.app.state.session_factory, api.app.state.redis))
    await ends(api, pool, second)
    [killed] = await rows(api, first)
    assert killed.saga_state == "escalated"
    # A's row moves to Waiting on you, and the master's card of it changes: both are said.
    assert (str(first.id), "inbox.action_required") in heard and (str(master.id), "worker.complete") in heard
    [found] = await thread_rows(api, project, thread_id=str(first.id))
    assert (found["group"], found["reason"]) == ("waiting", "files")


async def test_every_holder_of_the_lock_in_a_threads_work_settles_with_its_workers_redis(api, monkeypatch, tmp_path):
    master = await master_of(api, await create(api))
    thread = await a_coordinating_thread(api, master)
    pods = stored(api, thread, tmp_path)
    store, mine, theirs = api.app.state.session_store, SandboxPool(pods), SandboxPool(pods)
    #: Each of the pod's actions made under a lock whose holder settled first, and whether it settled with Redis.
    settled_for: dict[str, bool] = {}
    with_redis: list[bool] = []
    helpers: set[str] = set()
    settle, call = landing_module.settle_running, landing_module._call

    async def settled(session_factory, sandbox_pool, *args, **kwargs):
        had = kwargs.get("redis") is api.app.state.redis
        if isinstance(sandbox_pool, landing_module._Released):
            settled_for["prune"] = had  # the pruning asks its pod past the pool's sessions
        else:
            with_redis.append(had)
        return await settle(session_factory, sandbox_pool, *args, **kwargs)

    async def called(sandbox_pool, owner, action, **arguments):
        if with_redis:
            # The master's pod is asked by its routine's run alone.
            settled_for.setdefault(action if owner == str(thread.id) or owner in helpers else f"a routine's {action}", with_redis.pop())
        return await call(sandbox_pool, owner, action, **arguments)

    monkeypatch.setattr(landing_module, "settle_running", settled)
    monkeypatch.setattr(landing_module, "_call", called)

    async def stopped_while_the_helper_works(harness):
        [helper] = await helpers_of(api, thread)
        await edited(theirs, helper, "echo by the helper > sources.md")
        await stop(harness)

    # A step hands the copy on to a helper, and a stop drops that hand-off.
    await asyncio.wait_for(a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "echo outline > outline.md"})),
        calling(("spawn_worker", {"goal": "Draft the sources."})),
        calling(("memory", {"action": "add", "content": "x"})),
        _final_response("Done."),
    ], pool=mine, during=stopped_while_the_helper_works), 120)
    # The helper hands back at its end; the thread's next turn lands, and the day's pruning follows it.
    [helper] = await helpers_of(api, thread)
    helpers.add(str(helper.id))
    await ends(api, theirs, helper)
    await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "Something else."})
    await a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "echo other > other.md"})), _final_response("Done."),
    ], pool=SandboxPool(pods))
    await asyncio.gather(*landing_module._PRUNINGS)
    # A turn that fails keeps its copy.
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo half > half.md")
    await ends(api, pool, thread, failed=True)
    # A routine's run picks up what it changed, through the master's pod.
    run, _ = await a_routine_run(api, master, "Tidy up")
    await in_its_pod(api, pool, run, "echo tidied > notes.txt")
    await ends(api, pool, run)
    assert settled_for == {
        "hand_off": True, "drop_hand_off": True, "hand_back": True, "pickup": True, "prune": True, "keep": True,
        "a routine's pickup": True,
    }


async def test_a_files_history_shows_you_and_each_thread_newest_first(api, tmp_path):
    project, first, second, pods, pool = await two_threads(api, tmp_path)
    await edited(pool, second, "printf ' by B' >> Report.docx")
    # Your save after B's copy was made: B's version does not land, and the landing picks yours up first.
    (pods.project / "Report.docx").write_bytes(b"PK\x03\x04 report v1 by A, then by you")
    await ends(api, pool, second)
    listed = await history_of(api, project, "Report.docx")
    assert [(v["by"], v["change"], v["merged"], v["available"]) for v in listed] == [
        ({"kind": "thread", "thread_id": str(second.id), "title": "Draft B"}, "changed", False, True),
        ({"kind": "you"}, "changed", True, True),
        ({"kind": "thread", "thread_id": str(first.id), "title": "Draft A"}, "changed", True, True),
        # The file as it was before any of them: your upload.
        ({"kind": "you"}, "added", True, True),
    ]
    [by_a], [by_b] = await rows(api, first), await rows(api, second)
    assert [v["id"] for v in listed] == [f"{by_b.id}:f", f"{by_b.id}:p", f"{by_a.id}:f", f"{by_a.id}:b"]
    # Only a landed version came with a landing; every time is UTC with its Z, and each version is of the file asked.
    assert [v["landing_id"] for v in listed] == [None, None, str(by_a.id), None]
    assert all(v["at"].endswith("Z") and v["path"] == "Report.docx" for v in listed)
    assert sorted(listed[0]) == ["at", "available", "by", "change", "id", "landing_id", "merged", "path"]
    assert await history_of(api, project, "never-made.md") == []


async def test_a_files_first_version_your_upload_is_listed_after_two_changes_landed(api, tmp_path):
    project, first, second, pods, pool = await two_threads(api, tmp_path)
    await edited(pool, second, "printf ' by B' >> Report.docx")
    await ends(api, pool, second)
    [by_b, by_a, upload] = await history_of(api, project, "Report.docx")
    [landing] = await rows(api, first)
    # It has no record of its own: it is what the file's oldest record started from, no later than that record.
    assert upload == {
        "id": f"{landing.id}:b", "path": "Report.docx", "by": {"kind": "you"}, "at": upload["at"], "change": "added",
        "merged": True, "landing_id": None, "available": True,
    }
    assert (upload["at"], by_a["at"]) == (utc(landing.created_at), utc(landing.updated_at))
    assert upload["at"] < by_a["at"] <= by_b["at"]
    # A file a thread made has no version from before it; one it then took away is listed as deleted, with nothing to keep.
    # A landing of two files is a version of each, and of no other.
    await edited(pool, first, "echo a > a.md && printf ' more' >> notes.txt")
    await ends(api, pool, first)
    assert [v["path"] for v in await history_of(api, project, "notes.txt")] == ["notes.txt", "notes.txt"]
    [made] = await history_of(api, project, "a.md")
    assert (made["change"], made["by"]["kind"]) == ("added", "thread")
    await edited(pool, first, "rm a.md")
    await ends(api, pool, first)
    [gone, made] = await history_of(api, project, "a.md")
    assert (gone["change"], gone["available"], made["change"]) == ("deleted", True, "added")


async def test_a_pruned_version_is_listed_as_no_longer_kept(api, tmp_path):
    project, first, second, pods, pool = await two_threads(api, tmp_path)
    await edited(pool, second, "printf ' by B' >> Report.docx")
    await ends(api, pool, second)
    assert [v["available"] for v in await history_of(api, project, "Report.docx")] == [True, True, True]
    cut_history(tmp_path, pods.project / "_history", kept=1)
    listed = await history_of(api, project, "Report.docx")
    # Who made each and when is still the records': only its bytes are gone.
    assert [(v["by"]["kind"], v["available"]) for v in listed] == [("thread", True), ("thread", False), ("you", False)]


async def test_a_routines_change_is_listed_as_by_the_routine_and_came_with_no_landing(api, tmp_path):
    project, thread, pods, pool, row = await a_landing(api, tmp_path, "printf ' by A' >> Report.docx")
    master = await master_of(api, project)
    run, _ = await a_routine_run(api, master, "Tidy up")
    await in_its_pod(api, pool, run, "true")
    # As the run's calls wrote them: its record is a pickup of no thread.
    (pods.project / "Report.docx").write_bytes(b"PK\x03\x04 report v1 by A, tidied")
    (pods.project / "notes.txt").write_text("v2 notes, tidied\n")
    await ends(api, pool, run)
    [tidied] = await pickups_of(api, master)
    assert tidied.thread_id is None
    listed = await history_of(api, project, "Report.docx")
    assert [(v["id"], v["by"], v["change"], v["landing_id"]) for v in listed] == [
        (f"{tidied.id}:p", {"kind": "routine", "name": "Tidy up"}, "changed", None),
        (f"{row.id}:f", {"kind": "thread", "thread_id": str(thread.id), "title": "Draft A"}, "changed", str(row.id)),
        (f"{row.id}:b", {"kind": "you"}, "added", None),
    ]
    # A file only the routine changed: its version, and the file as you uploaded it.
    assert [(v["id"], v["by"], v["change"]) for v in await history_of(api, project, "notes.txt")] == [
        (f"{tidied.id}:p", {"kind": "routine", "name": "Tidy up"}, "changed"),
        (f"{tidied.id}:b", {"kind": "you"}, "added"),
    ]


async def test_a_files_history_answers_only_its_owner_and_only_in_its_own_project(api, session_factory, tmp_path):
    project, first, *_ = await two_threads(api, tmp_path)
    assert len(await history_of(api, project, "Report.docx")) == 2
    # Another user of the organisation: the project is none of theirs, and no path, author or time of it is answered.
    _, their_token = await add_user(session_factory, api.org_id)
    refused = await history_of(api, project, "Report.docx", their_token, status=404)
    assert refused == {"detail": "No such project."}
    # The owner, through another project of theirs: a file of the first is none of the second's, by its name or by a
    # path that climbs towards it.  A path is only ever a name among the project's own records.
    other = await create(api)
    master = await master_of(api, project)
    prefix = boundary_workspace_prefix(master.config, master, master.id)
    for path in ("Report.docx", f"../../../{prefix}Report.docx", f"/{prefix}Report.docx", "_history/packed-refs"):
        assert await history_of(api, other, path) == []
    # A file of the same name in each has each its own History.
    thread = await a_thread(api, "Draft C", await master_of(api, other))
    (tmp_path / "other").mkdir()
    pool = SandboxPool(stored(api, thread, tmp_path / "other"))
    await edited(pool, thread, "printf ' by C' >> Report.docx")
    await ends(api, pool, thread)
    assert [v["by"].get("title") for v in await history_of(api, other, "Report.docx")] == ["Draft C", None]
    assert [v["by"].get("title") for v in await history_of(api, project, "Report.docx")] == ["Draft A", None]


async def test_the_caller_names_a_file_and_the_storage_is_asked_for_none_by_that_name(api, monkeypatch, tmp_path):
    project, *_ = await two_threads(api, tmp_path)
    storage, asked = api.app.state.storage, []
    for name in ("download", "read", "list_entries", "stat", "exists"):
        def spied(bucket, key="", *args, _method=getattr(storage, name), **kwargs):
            asked.append(key)
            return _method(bucket, key, *args, **kwargs)

        monkeypatch.setattr(storage, name, spied)
    monkeypatch.setattr(rows_module, "_COUNTED", {})
    assert await history_of(api, project, "../../Report.docx") == []
    assert len(await history_of(api, project, "Report.docx")) == 2
    master = await master_of(api, project)
    prefix = boundary_workspace_prefix(master.config, master, master.id)
    # The project's own files, counted against the cap, and its own history: nothing the caller named.
    assert asked and all(key.startswith(prefix) for key in asked)
    assert all("Report.docx" not in key or key == f"{prefix}Report.docx" for key in asked)
    assert not any(".." in key for key in asked)


async def test_only_the_clouds_completed_records_are_a_files_versions(api, tmp_path):
    project, thread, pods, pool, row = await a_landing(api, tmp_path, "printf ' by A' >> Report.docx")
    [landed, upload] = await history_of(api, project, "Report.docx")
    other = await create(api)
    async with api.app.state.session_factory() as db:
        # A record of the same file still running, one put back, one of another project and one of a computer's folder.
        for saga, state, device, of in (
            ("saga:running", "running", "NULL", "workstream_id"), ("saga:back", "compensated", "NULL", "workstream_id"),
            ("saga:stuck", "escalated", "NULL", "workstream_id"), ("saga:elsewhere", "completed", "NULL", ":other"),
            ("saga:computer", "completed", "gen_random_uuid()", "workstream_id"),
        ):
            await db.execute(text(
                "INSERT INTO workstream_history (workstream_id, device_id, kind, saga_id, saga_state, thread_id, agent_id, steps, files) "
                f"SELECT {of}, {device}, 'landing', :saga, :state, thread_id, agent_id, steps, files "
                "FROM workstream_history WHERE id = :id"
            ), {"saga": f"{saga}:{row.id}", "state": state, "id": row.id, **({"other": other["id"]} if of == ":other" else {})})
        # The computer's version is one the cloud's history never held.
        await db.execute(text(
            "UPDATE workstream_history SET files = jsonb_set(files, '{0,after}', to_jsonb(repeat('c', 40))) WHERE saga_id = :saga"
        ), {"saga": f"saga:computer:{row.id}"})
        await db.commit()
        computer = (await db.execute(
            text("SELECT device_id, id FROM workstream_history WHERE saga_id = :saga"), {"saga": f"saga:computer:{row.id}"},
        )).one()
    assert await history_of(api, project, "Report.docx") == [landed, upload]
    # A computer's folder has records of its own, and its computer holds its versions: the api's copy is not asked.
    [theirs, before] = await history_of(api, project, "Report.docx", device_id=str(computer.device_id))
    assert (theirs["id"], before["id"], theirs["available"]) == (f"{computer.id}:f", f"{computer.id}:b", True)
    assert await history_of(api, project, "Report.docx", device_id="00000000-0000-4000-8000-000000000000") == []


async def test_a_project_over_the_file_cap_says_its_history_is_off(api, monkeypatch, tmp_path):
    project, *_ = await a_landing(api, tmp_path, "printf ' by A' >> Report.docx")
    monkeypatch.setattr(rows_module, "HISTORY_CAP", 1)  # its two files are over it
    monkeypatch.setattr(rows_module, "_COUNTED", {})
    refused = await history_of(api, project, "Report.docx", status=409)
    assert refused["detail"] == "History is off: this project has more than 50,000 files."
    # A folder on a computer is counted by its computer, not by the cloud's files.
    assert await history_of(api, project, "Report.docx", device_id="00000000-0000-4000-8000-000000000000") == []
    # The copy is not brought in to say so.
    assert not list((Path(api.app.state.settings.history.copies_path)).glob("*/objects/pack/*.pack"))


@pytest.mark.parametrize("path", ["a\x00b", "", "a" * 4097, None], ids=["a NUL", "empty", "longer than a path may be", "none"])
async def test_a_path_no_file_can_have_is_refused_before_it_is_read(api, tmp_path, path):
    project = await create(api)
    response = await api.client.get(
        f"/v1/workstreams/{project['id']}/history", params={} if path is None else {"path": path}, headers=api.auth(),
    )
    assert response.status_code == 422, response.text
    # The longest path the shell takes is asked as it is, never trimmed.
    assert await history_of(api, project, " " + "a" * 4095) == []


async def test_a_history_the_api_cannot_read_is_said_in_words(api, monkeypatch, tmp_path):
    project, *_ = await two_threads(api, tmp_path)
    assert len(await history_of(api, project, "Report.docx")) == 2

    async def a_pack_went(self, blobs):
        raise HistoryError("git cat-file failed: fatal: packfile /srv/copies/ab12 is gone")

    with monkeypatch.context() as patched:
        patched.setattr(BucketHistory, "held", a_pack_went)
        unread = await history_of(api, project, "Report.docx", status=503)
    # Git's own words stay in the log.
    assert unread["detail"] == "The project's history could not be read just now. Try again in a moment."
    # A history larger than the api copies is refused before any of it is read, and History says why it shows nothing.
    with monkeypatch.context() as patched:
        patched.setattr(api.app.state.settings.history, "copies_path", str(tmp_path / "other-copies"))
        patched.setattr(api.app.state.settings.history, "packs_bound", 16)
        too_large = "This project's history is larger than Surogate can read here."
        assert (await history_of(api, project, "Report.docx", status=409))["detail"] == too_large
        assert not list((tmp_path / "other-copies").glob("*/objects/pack/*.pack"))
    # Nothing is said of versions the records hold when the copy could not say which are kept: no list, never a guess.
    assert len(await history_of(api, project, "Report.docx")) == 2


async def test_a_history_another_request_is_bringing_in_is_waited_for_then_said_to_be_busy(api, monkeypatch, tmp_path):
    project, *_ = await two_threads(api, tmp_path)
    assert len(await history_of(api, project, "Report.docx")) == 2
    place = BucketHistory.of(api.app.state.storage, await master_of(api, project), api.app.state.settings.history)
    monkeypatch.setattr(bucket_module, "_PATIENCE", 0.3)
    held = os.open(bucket_module._lock_of(place.clone), os.O_RDWR)
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        response = await api.client.get(
            f"/v1/workstreams/{project['id']}/history", params={"path": "Report.docx"}, headers=api.auth(),
        )
    finally:
        os.close(held)
    assert (response.status_code, response.headers["retry-after"]) == (503, "5")
    assert response.json()["detail"] == "This project's history is being read just now. Try again in a moment."
    assert len(await history_of(api, project, "Report.docx")) == 2


async def test_a_file_with_more_versions_than_the_shell_takes_answers_its_newest(api, tmp_path):
    project, thread, pods, pool, row = await a_landing(api, tmp_path, "printf ' by A' >> Report.docx")
    kept = row.files[0]["after"]
    async with api.app.state.session_factory() as db:
        # Six hundred more landings of the file, each recorded as the first was.
        await db.execute(text(
            "INSERT INTO workstream_history (workstream_id, kind, saga_id, saga_state, thread_id, agent_id, steps, files) "
            "SELECT workstream_id, 'landing', 'saga:more:' || id || ':' || n, 'completed', thread_id, agent_id, steps, "
            "jsonb_build_array(jsonb_build_object('path', 'Report.docx', 'before', cast(:kept AS text), 'after', cast(:kept AS text), 'merged', true)) "
            "FROM workstream_history, generate_series(1, 600) n WHERE id = :id"
        ), {"kept": kept, "id": row.id})
        await db.commit()
        newest = (await db.execute(text("SELECT max(id) FROM workstream_history WHERE thread_id = :t"), {"t": thread.id})).scalar_one()
    listed = await history_of(api, project, "Report.docx")
    assert len(listed) == 500
    assert [listed[0]["id"], listed[-1]["id"]] == [f"{newest}:f", f"{newest - 499}:f"]
    assert all(v["available"] for v in listed)


async def test_open_version_answers_the_file_as_that_version_left_it_as_data_to_save(api, tmp_path):
    project, first, second, pods, pool = await two_threads(api, tmp_path)
    await edited(pool, second, "printf ' by B' >> Report.docx")
    # Your save after B's copy was made: B's version does not land, and the landing picks yours up first.
    (pods.project / "Report.docx").write_bytes(b"PK\x03\x04 report v1 by A, then by you")
    await ends(api, pool, second)
    [by_b, yours, by_a, upload] = await history_of(api, project, "Report.docx")
    response = await opened(api, project, by_a["id"], "Report.docx")
    assert (response.status_code, response.content) == (200, b"PK\x03\x04 report v1 by A")
    # Data to save under the file's own name, whatever it holds: never a page to show.
    said = ("content-type", "content-length", "x-content-type-options", "content-disposition")
    assert [response.headers[name] for name in said] == [
        "application/octet-stream", "19", "nosniff", "attachment; filename*=UTF-8''Report.docx",
    ]
    # Every version the History lists opens as it was: a thread's that did not land, your own edit, and your upload.
    assert [(await opened(api, project, v["id"], "Report.docx")).content for v in (by_b, yours, upload)] == [
        b"PK\x03\x04 report v1 by A by B", b"PK\x03\x04 report v1 by A, then by you", b"PK\x03\x04 report v1",
    ]
    # What the file was before a record that picked it up and then changed it: before the pickup, which came first.
    before = await opened(api, project, f"{by_b['id'].split(':')[0]}:b", "Report.docx")
    assert (before.status_code, before.content) == (200, b"PK\x03\x04 report v1 by A")
    # What went to the client is gone from the api's copy.
    assert written_out(await copy_of(api, project)) == []


async def test_a_version_opens_only_for_its_owner_as_a_version_the_projects_records_name_for_that_file_and_nothing_else_reaches_git(
    api, session_factory, monkeypatch, tmp_path,
):
    project, first, second, pods, pool = await two_threads(api, tmp_path)
    await edited(pool, first, "echo a > a.md")
    await ends(api, pool, first)
    [by_a, upload] = await history_of(api, project, "Report.docx")
    [made] = await history_of(api, project, "a.md")
    [row, later] = await rows(api, first)
    report = row.files[0]["after"]
    reached, version = [], BucketHistory.version

    async def spied(self, blob):
        reached.append(blob)
        return await version(self, blob)

    monkeypatch.setattr(BucketHistory, "version", spied)
    # Another user of the organisation: the project is none of theirs, whatever of it they name.
    _, their_token = await add_user(session_factory, api.org_id)
    for refused in (await opened(api, project, by_a["id"], "Report.docx", their_token), await opened(api, project, "0:f", "x", their_token)):
        assert (refused.status_code, refused.json()) == (404, {"detail": "No such project."})
    assert await deleted_of(api, project, their_token, status=404) == {"detail": "No such project."}
    # The owner, through another project of theirs; and records of this file that are no versions of the project's
    # cloud files: one still running, one put back, one that could not be, and one of a folder on a computer.
    other = await create(api)
    entry = [{"path": "Report.docx", "before": row.files[0]["before"], "after": report, "merged": True}]
    odd = [
        await recorded(api, row, saga=saga, files=entry, **columns) for saga, columns in (
            ("saga:running", {"saga_state": "'running'"}), ("saga:back", {"saga_state": "'compensated'"}),
            ("saga:stuck", {"saga_state": "'escalated'"}), ("saga:computer", {"device_id": "gen_random_uuid()"}),
        )
    ]
    elsewhere = await recorded(api, row, saga="saga:elsewhere", files=entry, workstream_id=f"'{other['id']}'")
    none_of_these = [
        (other, by_a["id"], "Report.docx"),  # a version of another project
        (project, f"{elsewhere}:f", "Report.docx"),  # another project's record, named through this one
        (project, made["id"], "Report.docx"),  # another file's version
        (project, by_a["id"], "a.md"),  # this version, as another file's
        (project, by_a["id"], "./Report.docx"),
        (project, by_a["id"], "Report.docx/"),
        (project, f"{row.id}:p", "Report.docx"),  # nothing of it was picked up
        (project, f"{later.id}:b", "a.md"),  # a file a thread made has no version from before it
        *((project, f"{record}:{side}", "Report.docx") for record in odd for side in "fb"),
        # What the copy holds, named as itself: a commit, and the version's own bytes.  A version is named by its record.
        (project, row.commit, "Report.docx"),
        (project, report, "Report.docx"),
        (project, f"{row.commit}:Report.docx", "Report.docx"),
    ]
    for of, named, path in none_of_these:
        refused = await opened(api, of, named, path)
        assert (refused.status_code, refused.json()) == (404, {"detail": "No such version."}), (named, path)
    assert reached == []
    assert (await opened(api, project, by_a["id"], "Report.docx")).content == b"PK\x03\x04 report v1 by A"
    assert reached == [report]


async def test_a_version_that_names_no_record_is_no_version(api, tmp_path):
    project, thread, pods, pool, row = await a_landing(api, tmp_path, "printf ' by A' >> Report.docx")
    assert (await opened(api, project, f"{row.id}:f", "Report.docx")).status_code == 200
    for named in (
        "²:f", "１:f", "9" * 18 + ":f", "9" * 19 + ":f", "9" * 5000 + ":f", ":f", str(row.id), f"{row.id}:", f"{row.id}:x",
        f"{row.id}:F", f"{row.id}:f:f", f"{row.id}:fb", f" {row.id}:f", f"{row.id}:f ", f"{row.id}\n:f", f"+{row.id}:f", f"-{row.id}:f",
        f"{row.id}.0:f", f"0x{row.id:x}:f",
    ):
        refused = await opened(api, project, named, "Report.docx")
        assert (refused.status_code, refused.json()) == (404, {"detail": "No such version."}), named
    # One that would be more than a part of the address is no address of a version at all.
    assert (await opened(api, project, f"{row.id}:f/../{row.id}:f", "Report.docx")).status_code == 404
    # A path no file can have is refused before any record is read.
    for path in ("a\x00b", "", "a" * 4097, None):
        assert (await opened(api, project, f"{row.id}:f", path)).status_code == 422, path


async def test_a_pruned_version_answers_that_it_is_no_longer_kept(api, tmp_path):
    project, first, second, pods, pool = await two_threads(api, tmp_path)
    await edited(pool, second, "printf ' by B' >> Report.docx")
    await ends(api, pool, second)
    cut_history(tmp_path, pods.project / "_history", kept=1)
    listed = await history_of(api, project, "Report.docx")
    assert [v["available"] for v in listed] == [True, False, False]
    for gone in listed[1:]:
        response = await opened(api, project, gone["id"], "Report.docx")
        assert (response.status_code, response.json()) == (410, {"detail": "This version is no longer kept in the project's history."})
    assert (await opened(api, project, listed[0]["id"], "Report.docx")).content == b"PK\x03\x04 report v1 by A by B"
    assert written_out(await copy_of(api, project)) == []


async def test_a_version_of_a_page_or_a_drawing_is_data_and_its_name_is_only_a_name(api, tmp_path):
    project, thread, pods, pool, row = await a_landing(
        api, tmp_path, "printf '<script>alert(1)</script>' > page.html && printf '<svg onload=\"alert(1)\"/>' > drawing.svg",
    )
    blobs = {f["path"]: f["after"] for f in row.files}
    for name, held in (("page.html", b"<script>alert(1)</script>"), ("drawing.svg", b'<svg onload="alert(1)"/>')):
        response = await opened(api, project, f"{row.id}:f", name)
        assert (response.status_code, response.content) == (200, held)
        assert (response.headers["content-type"], response.headers["x-content-type-options"]) == ("application/octet-stream", "nosniff")
        assert response.headers["content-disposition"] == f"attachment; filename*=UTF-8''{name}"
    # A file's path is data a thread's command chose.  What is saved is named by its last part alone, with no
    # character that ends a header or steers a terminal, and no longer than a file's name may be.
    names = {
        "reports/2026/Raport final – ș.docx": "Raport final – ș.docx",
        'evil"; filename="x.html': 'evil"; filename="x.html',
        "line\r\nSet-Cookie: session=theirs\r\n\r\n<script>.html": "lineSet-Cookie: session=theirs<script>.html",
        "..\\..\\AppData\\run.bat": ".._.._AppData_run.bat",
        "tab\there\x7f\x1b[2J.txt": "tabhere[2J.txt",
        "é" * 200 + ".docx": "é" * 125 + ".docx",
        "a" * 300: "a" * 255,
        "x." + "y" * 300: ("x." + "y" * 300)[:255],
        "folder/..": "file", "folder/.": "file", "folder/": "file", "...": "file", "\x01\x02": "file",
    }
    made = await recorded(api, row, saga="saga:names", files=[
        {"path": path, "before": None, "after": blobs["page.html"], "merged": True} for path in names
    ])
    for path, name in names.items():
        response = await opened(api, project, f"{made}:f", path)
        assert (response.status_code, response.content) == (200, b"<script>alert(1)</script>"), path
        disposition = response.headers["content-disposition"]
        assert re.fullmatch(r"attachment; filename\*=UTF-8''[A-Za-z0-9._~%-]+", disposition), disposition
        assert unquote(disposition.partition("''")[2]) == name
        assert len(name.encode()) <= 255 and response.headers["content-type"] == "application/octet-stream"
        assert "set-cookie" not in response.headers
    assert written_out(await copy_of(api, project)) == []


async def test_a_version_is_sent_a_piece_at_a_time_and_is_gone_from_the_copy_however_its_sending_ends(api, tmp_path):
    project, thread, pods, pool, row = await a_landing(api, tmp_path, "head -c 2621440 /dev/urandom > video.mp4")
    data, place = (pods.project / "video.mp4").read_bytes(), await copy_of(api, project)
    scope = {"type": "http", "method": "GET"}

    async def sending(send) -> None:
        staged = await place.version(row.files[0]["after"])
        assert len(written_out(place)) == 1
        await routes_module._VersionFile(staged, "video.mp4")(scope, None, send)

    said = []

    async def kept(message) -> None:
        said.append(message)

    await sending(kept)
    start, *pieces, last = said
    assert (start["type"], start["status"], dict(start["headers"])[b"content-length"]) == ("http.response.start", 200, b"2621440")
    # The response's body is the version, no piece larger than is read at once, and its end is said.
    assert [(len(piece["body"]), piece["more_body"]) for piece in pieces] == [(2**20, True), (2**20, True), (2**19, True)]
    assert (b"".join(piece["body"] for piece in pieces), last) == (data, {"type": "http.response.body", "body": b""})
    assert written_out(place) == []

    # A client that left: its connection closed under the response, or its request ended by the server.
    async def closed(message) -> None:
        if message["type"] == "http.response.body":
            raise OSError("the client closed its connection")

    with pytest.raises(OSError, match="closed its connection"):
        await sending(closed)
    assert written_out(place) == []

    async def stalled(message) -> None:
        if message["type"] == "http.response.body":
            await asyncio.sleep(30)

    leaving = asyncio.create_task(sending(stalled))
    for _ in range(100):
        if written_out(place):
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.1)
    leaving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leaving
    assert written_out(place) == []

    async def refused(message) -> None:
        raise OSError("the client closed its connection")

    # Even one that left before a word of the response was said.
    with pytest.raises(OSError):
        await sending(refused)
    assert (written_out(place), place.clone in bucket_module._USING) == ([], False)
    assert (await opened(api, project, f"{row.id}:f", "video.mp4")).content == data


async def test_a_version_the_api_cannot_write_out_is_said_in_words(api, monkeypatch, tmp_path):
    project, thread, pods, pool, row = await a_landing(api, tmp_path, "printf ' by A' >> Report.docx")
    named = f"{row.id}:f"
    assert (await opened(api, project, named, "Report.docx")).status_code == 200
    place = await copy_of(api, project)
    # Larger than a version may be: refused as the bounds are, with nothing written.
    with monkeypatch.context() as patched:
        patched.setattr(api.app.state.settings.history, "file_bound", 18)
        response = await opened(api, project, named, "Report.docx")
    assert (response.status_code, response.json()) == (409, {"detail": "This version is larger than Surogate can read here."})
    # A history larger than the api copies.
    with monkeypatch.context() as patched:
        patched.setattr(api.app.state.settings.history, "copies_path", str(tmp_path / "other-copies"))
        patched.setattr(api.app.state.settings.history, "packs_bound", 16)
        response = await opened(api, project, named, "Report.docx")
        assert (response.status_code, response.json()) == (409, {"detail": "This project's history is larger than Surogate can read here."})
    # The copy another request has, past what this one waits: told to try again, and when.
    with monkeypatch.context() as patched:
        patched.setattr(bucket_module, "_PATIENCE", 0.3)
        held = os.open(bucket_module._lock_of(place.clone), os.O_RDWR)
        fcntl.flock(held, fcntl.LOCK_EX)
        try:
            response = await opened(api, project, named, "Report.docx")
        finally:
            os.close(held)
    assert (response.status_code, response.headers["retry-after"]) == (503, "5")
    assert response.json() == {"detail": "This project's history is being read just now. Try again in a moment."}

    # Git's own words, and a disk's, stay in the log.
    for failure in (HistoryError("git cat-file failed: fatal: packfile /srv/copies/ab12 is gone"), OSError(28, "No space left on device")):
        async def failed(self, blob, failure=failure):
            raise failure

        with monkeypatch.context() as patched:
            patched.setattr(BucketHistory, "version", failed)
            response = await opened(api, project, named, "Report.docx")
        assert (response.status_code, response.json()) == (503, {"detail": "The project's history could not be read just now. Try again in a moment."})
    assert written_out(place) == []
    assert (await opened(api, project, named, "Report.docx")).status_code == 200


async def test_a_project_over_the_file_cap_opens_no_version_and_lists_no_deleted_file(api, monkeypatch, tmp_path):
    project, thread, pods, pool, row = await a_landing(api, tmp_path, "rm notes.txt && echo a > a.md")
    assert [v["path"] for v in (await deleted_of(api, project))["files"]] == ["notes.txt"]
    monkeypatch.setattr(rows_module, "HISTORY_CAP", 1)  # its two files are over it
    monkeypatch.setattr(rows_module, "_COUNTED", {})
    response = await opened(api, project, f"{row.id}:b", "notes.txt")
    assert (response.status_code, response.json()) == (409, {"detail": "History is off: this project has more than 50,000 files."})
    # Nor does it list a deleted file, whose History it could not show.
    assert await deleted_of(api, project) == {"files": [], "more": False}
    assert not list((Path(api.app.state.settings.history.copies_path)).glob("*/objects/pack/*.pack"))


async def test_a_deleted_file_is_listed_so_that_its_history_can_be_reached(api, tmp_path):
    project, thread, pods, pool, row = await a_landing(api, tmp_path, "rm notes.txt && printf ' by A' >> Report.docx")
    listed = await deleted_of(api, project)
    assert listed == {"files": [{
        "id": f"{row.id}:f", "path": "notes.txt", "by": {"kind": "thread", "thread_id": str(thread.id), "title": "Draft A"},
        "at": utc(row.updated_at), "change": "deleted", "merged": True, "landing_id": str(row.id), "available": True,
    }], "more": False}
    # Its History opens as any file's: the deletion, with nothing to open, and the version it took away.
    [gone, upload] = await history_of(api, project, "notes.txt")
    assert (gone, upload["change"], upload["id"]) == (listed["files"][0], "added", f"{row.id}:b")
    nothing = await opened(api, project, gone["id"], "notes.txt")
    assert (nothing.status_code, nothing.json()) == (404, {"detail": "This version deleted the file: there is nothing to open."})
    assert (await opened(api, project, upload["id"], "notes.txt")).content == b"v1 notes\n"
    # A file you deleted is yours to find too, once a landing picks the deletion up; one made again is no longer gone.
    (pods.project / "Report.docx").unlink()
    await edited(pool, thread, "echo again > notes.txt")
    await ends(api, pool, thread)
    [_, picked] = await rows(api, thread)
    assert [(v["id"], v["path"], v["by"], v["change"], v["landing_id"]) for v in (await deleted_of(api, project))["files"]] == [
        (f"{picked.id}:p", "Report.docx", {"kind": "you"}, "deleted", None),
    ]
    # Made again by you, it is gone no more once that is picked up.
    (pods.project / "Report.docx").write_bytes(b"PK\x03\x04 report, uploaded again")
    await edited(pool, thread, "echo more >> notes.txt")
    await ends(api, pool, thread)
    assert await deleted_of(api, project) == {"files": [], "more": False}


async def test_the_deleted_files_are_those_the_clouds_landed_records_took_away_the_newest_first_and_said_to_be_more_than_are_listed(
    api, monkeypatch, tmp_path,
):
    project, thread, pods, pool, row = await a_landing(api, tmp_path, "rm notes.txt")
    other = await create(api)
    kept = row.files[0]["before"]

    def gone(*paths: str, merged: bool = True) -> list[dict]:
        return [{"path": path, "before": kept, "after": None, "merged": merged} for path in paths]

    # Deletions that took no file of the project's cloud files away: one still running, one put back, one that could
    # not be, one of another project, one of a folder on a computer; and a thread's deletion that did not land.
    for saga, columns in (
        ("saga:running", {"saga_state": "'running'"}), ("saga:back", {"saga_state": "'compensated'"}),
        ("saga:stuck", {"saga_state": "'escalated'"}), ("saga:computer", {"device_id": "gen_random_uuid()"}),
        ("saga:elsewhere", {"workstream_id": f"'{other['id']}'"}),
    ):
        await recorded(api, row, saga=saga, files=gone(f"{saga}.md"), **columns)
    await recorded(api, row, saga="saga:unlanded", files=gone("unlanded.md", merged=False))
    assert [v["path"] for v in (await deleted_of(api, project))["files"]] == ["notes.txt"]
    assert [v["path"] for v in (await deleted_of(api, other))["files"]] == ["saga:elsewhere.md"]
    # More files gone since, two of them picked up as your own deletions: the newest first, a record's in their names' order.
    first = await recorded(api, row, saga="saga:more-1", files=gone("b.md", "a.md"), picked_up=gone("yours.md"))
    second = await recorded(api, row, saga="saga:more-2", picked_up=gone("c.md"))
    listed = await deleted_of(api, project)
    assert [(v["id"], v["path"], v["by"]["kind"], v["landing_id"]) for v in listed["files"]] == [
        (f"{second}:p", "c.md", "you", None), (f"{first}:f", "a.md", "thread", str(first)), (f"{first}:f", "b.md", "thread", str(first)),
        (f"{first}:p", "yours.md", "you", None), (f"{row.id}:f", "notes.txt", "thread", str(row.id)),
    ]
    assert listed["more"] is False
    # A record that picked a file up and then changed it again: what its files did is the newer.  A file you
    # deleted that the thread then made again is there; one you changed that the thread then took away is gone, by the thread.
    both = await recorded(
        api, row, saga="saga:both",
        picked_up=[*gone("yours.md"), {"path": "taken.md", "before": kept, "after": kept}],
        files=[{"path": "yours.md", "before": None, "after": kept, "merged": True}, *gone("taken.md")],
    )
    listed = await deleted_of(api, project)
    assert [(v["id"], v["path"], v["by"]["kind"]) for v in listed["files"]] == [
        (f"{both}:f", "taken.md", "thread"), (f"{second}:p", "c.md", "you"), (f"{first}:f", "a.md", "thread"),
        (f"{first}:f", "b.md", "thread"), (f"{row.id}:f", "notes.txt", "thread"),
    ]
    # No more of them than the shell takes: the newest, and that there are others is said.
    monkeypatch.setitem(SHELL_LIMITS, "deleted", 3)
    listed = await deleted_of(api, project)
    assert ([v["path"] for v in listed["files"]], listed["more"]) == (["taken.md", "c.md", "a.md"], True)
    monkeypatch.setitem(SHELL_LIMITS, "deleted", 5)
    assert (await deleted_of(api, project))["more"] is False
    # A file whose path is longer than the shell takes is left out, as the Library leaves it out.
    await recorded(api, row, saga="saga:long", files=gone("d/" * 2048 + "e.md"))
    listed = await deleted_of(api, project)
    assert ([v["path"] for v in listed["files"]], listed["more"]) == (["taken.md", "c.md", "a.md", "b.md"], True)
    # They are looked for among the project's latest records, and no further back however many it has: a file an
    # older record took away is listed no more, and that there may be such files is said.
    monkeypatch.setitem(SHELL_LIMITS, "deleted", 500)
    monkeypatch.setattr(rows_module, "_GONE_AMONG", 3)
    listed = await deleted_of(api, project)
    assert ([v["path"] for v in listed["files"]], listed["more"]) == (["taken.md", "c.md"], True)
    # With every record of the project among them, none is left out, and none is said to be.
    monkeypatch.setattr(rows_module, "_GONE_AMONG", 7)
    listed = await deleted_of(api, project)
    assert ([v["path"] for v in listed["files"]], listed["more"]) == (["taken.md", "c.md", "a.md", "b.md", "notes.txt"], False)
