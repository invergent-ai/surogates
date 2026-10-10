"""A project's files in its threads' rows: what each thread's landings changed, how each file
stands, and the stream's word of a landing another lock holder finished.  And a file's History in
the Library: its versions read from the project's records, each still kept or not."""

from __future__ import annotations

import asyncio
import fcntl
import os
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text

from surogates.harness import landing as landing_module
from surogates.sandbox.history import HistoryError
from surogates.session.store import SessionStore
from surogates.storage.tenant import boundary_workspace_prefix
from surogates.workstreams import bucket as bucket_module
from surogates.workstreams import history as rows_module
from surogates.workstreams import stream as project_stream
from surogates.workstreams.bucket import BucketHistory
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
from .test_thread_copies import a_thread, git, open_pod, pods  # noqa: F401  (pods is a fixture)
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


async def test_a_pushed_landing_whose_row_could_not_be_written_is_put_back_once_landed_over_and_never_announced(
    api, monkeypatch, tmp_path,
):
    """What holds today, not what should: the files of a landing that pushed are taken out of the
    project again, by the settle after one whose two writes of its row were refused."""
    project, first, second, pool = await a_landing_left_pushed(api, monkeypatch, tmp_path)
    told = announced(api, monkeypatch, second)
    save = landing_module.save_landing
    refused: list[int] = []

    async def unwritten(session_factory, row, saga, **values):
        if values.get("state") == "completed" and saga.session_id == second.id:
            refused.append(row)
            raise ConnectionError("the database went away")
        await save(session_factory, row, saga, **values)

    monkeypatch.setattr(landing_module, "save_landing", unwritten)
    await ends(api, pool, first)
    # The settle found it pushed, and its row was never written so: nothing is announced that a
    # reader would not find.  A's landing went over it, so the next settle, the pruning's, takes
    # it for not pushed and puts it back.
    assert len(refused) == 2 and told == []
    [killed] = await rows(api, second)
    assert (killed.saga_state, killed.files, killed.commit) == ("compensated", [], None)
    # B's file is gone from the project's files, while the history's main still holds B's landing
    # under A's, and its tree the file.  B's row lists nothing, so its thread's row shows no file.
    files = api.app.state.storage._resolve(first.config["storage_bucket"], boundary_workspace_prefix(first.config, first, first.id))
    durable = files / "_history"
    assert sorted(found.name for found in files.iterdir() if not found.name.startswith("_")) == ["Report.docx", "c.md", "notes.txt"]
    assert "b.md" in git(durable, "ls-tree", "--name-only", "refs/heads/main").split()
    assert f"Surogate-Saga: {killed.saga_id}" in git(durable, "log", "--format=%(trailers:only,unfold)", "refs/heads/main")
    assert await marks_of(api, project, second) == []


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
    assert upload["at"] <= by_a["at"] <= by_b["at"]
    # A file a thread made has no version from before it; one it then took away is listed as deleted, with nothing to keep.
    await edited(pool, first, "echo a > a.md")
    await ends(api, pool, first)
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
