"""A project's files in its threads' rows: what each thread's landings changed, and how each file stands."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from surogates.harness import landing as landing_module
from surogates.sandbox.pool import SandboxPool
from surogates.session.events import EventType
from tests.test_steer_loop import _final_response

from .test_devices import api  # noqa: F401  (api is a fixture)
from .test_durable_landings import a_short_fence, edited, ends, rows, stored  # noqa: F401  (a_short_fence is a fixture)
from .test_redo_loop import a_clash
from .test_thread_copies import a_thread, open_pod, pods  # noqa: F401  (pods is a fixture)
from .test_turn_sagas import a_turn, calling
from .test_workstream_overview import act_on
from .test_workstream_overview import rows as thread_rows
from .test_workstream_threads import call_tool
from .test_workstreams import create, master_of

pytestmark = pytest.mark.asyncio(loop_scope="session")

REQUEST = EventType.LLM_REQUEST


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


async def test_another_projects_landings_and_a_landing_left_running_mark_nothing(api, tmp_path):
    project, thread, pods, pool, row = await a_landing(api, tmp_path, "printf ' by A' >> Report.docx")
    other = await create(api)
    async with api.app.state.session_factory() as db:
        # A record of this thread under another project, and one still running: neither is this row's.
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
