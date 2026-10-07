"""A project's Overview over the API: its threads' rows, its counts, resolving
and reopening, its stream, and starting a proposed thread from its card."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest

from surogates.session.events import EventType
from surogates.workstreams.derive import SHELL_LIMITS

from .test_devices import add_user, api  # noqa: F401  (api is a fixture)
from .test_workstream_threads import (
    answered,
    asks,
    gives_up_asking,
    start,
    threads_in_every_state,
    turn_ends,
)
from .test_workstreams import create, master_of

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def rows(api, project: dict, token: str | None = None, **params) -> list[dict]:
    response = await api.client.get(
        f"/v1/workstreams/{project['id']}/threads", params=params, headers=api.auth(token),
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_the_overview_reads_where_each_thread_stands(api):
    project = await create(api)
    made = await threads_in_every_state(api, await master_of(api, project))
    listed = await rows(api, project)
    assert {row["title"]: (row["id"], row["group"], row["reason"], row["status_line"]) for row in listed} == {
        title: (str(made[title].id), *state) for title, state in {
            "Check the revenue figures": ("waiting", "question", "Which quarter's exchange rate should I use?"),
            "Send the draft to finance": ("waiting", "approval", "Send an email to finance@example.com?"),
            "Pick the year": ("waiting", "question", "Which year?"),
            "Convert the old reports": ("waiting", "failed", "The PDF could not be opened: it is encrypted"),
            "Draft the summary": ("working", None, None),
            "Tidy the shared folder": ("working", "computer", "Waiting for thinkpad"),
            "Collect the sales data": ("idle", None, "Did the work."),
            "Book the room": ("resolved", None, "Did the work."),
            "Book the review meeting": ("resolved", None, "Did the work."),
        }.items()
    }
    # Newest first, every time in UTC with its Z, and each thread's own files.
    moments = [datetime.fromisoformat(row["updated_at"]) for row in listed]
    assert moments == sorted(moments, reverse=True)
    assert all(row["created_at"].endswith("Z") and row["updated_at"].endswith("Z") for row in listed)
    sales = next(row for row in listed if row["title"] == "Collect the sales data")
    path = "threads/Collect the sales data/notes.md"
    assert (sales["files"], sales["place"], sales["resolved_at"]) == (
        [{"kind": "file", "label": path, "ref": path, "thread_id": sales["id"]}], {"kind": "cloud"}, None,
    )
    meeting = next(row for row in listed if row["title"] == "Book the review meeting")
    assert meeting["resolved_at"].endswith("Z")


async def test_a_working_threads_row_shows_its_progress_and_its_latest_step(api):
    project = await create(api)
    thread = await start(api, await master_of(api, project))
    store = api.app.state.session_store
    await store.emit_event(thread.id, EventType.TODO_UPDATED, {"todos": [
        {"id": "1", "content": "Outline", "status": "completed"},
        {"id": "2", "content": "Draft", "status": "in_progress"},
        {"id": "3", "content": "Charts", "status": "cancelled"},
    ]})
    await store.emit_event(thread.id, EventType.ITERATION_SUMMARY, {"summary": "Writing the outlook"})
    [row] = await rows(api, project)
    assert (row["group"], row["status_line"], row["progress"]) == ("working", "Writing the outlook", {"done": 1, "total": 2})


async def test_the_overview_reads_one_thread(api):
    project = await create(api)
    master = await master_of(api, project)
    first = await start(api, master, title="Draft A")
    await start(api, master, title="Draft B", goal="Draft B.")
    [row] = await rows(api, project, thread_id=str(first.id))
    assert (row["id"], row["title"], row["group"]) == (str(first.id), "Draft A", "working")
    # Another project's thread, and a deleted one, are no rows of this project's.
    other = await start(api, await master_of(api, await create(api, name="Budget")))
    assert await rows(api, project, thread_id=str(other.id)) == []
    await api.app.state.session_store.update_session_status(first.id, "archived")
    assert await rows(api, project, thread_id=str(first.id)) == []


async def test_the_overview_answers_no_more_rows_than_the_shell_takes(api, monkeypatch):
    monkeypatch.setitem(SHELL_LIMITS, "rows", 2)
    project = await create(api)
    master = await master_of(api, project)
    for title in ("Draft A", "Draft B", "Draft C"):
        await start(api, master, title=title, goal=f"{title}.")
    assert [row["title"] for row in await rows(api, project)] == ["Draft C", "Draft B"]


async def test_only_the_owner_reads_a_projects_threads(api, session_factory):
    project = await create(api)
    await start(api, await master_of(api, project))
    _, their_token = await add_user(session_factory, api.org_id)
    theirs = await api.client.get(f"/v1/workstreams/{project['id']}/threads", headers=api.auth(their_token))
    assert theirs.status_code == 404, theirs.text
    unknown = await api.client.get(f"/v1/workstreams/{uuid4()}/threads", headers=api.auth())
    assert unknown.status_code == 404, unknown.text


async def test_a_question_answered_in_the_thread_ends_its_wait(api):
    project = await create(api)
    thread = await start(api, await master_of(api, project))
    await asks(api, thread, "Which year?")
    [row] = await rows(api, project)
    assert (row["group"], row["reason"], row["status_line"]) == ("waiting", "question", "Which year?")
    response = await api.client.post(f"/v1/sessions/{thread.id}/messages", json={"content": "2025"}, headers=api.auth())
    assert response.status_code == 202, response.text
    # The typed message is the question's answer.
    assert await api.app.state.session_store.get_events(thread.id, types=[EventType.ASK_USER_QUESTION_RESPONSE])
    [row] = await rows(api, project)
    assert (row["group"], row["reason"]) == ("working", None)


async def test_a_question_that_expired_waits_until_the_reply(api):
    project = await create(api)
    thread = await start(api, await master_of(api, project))
    await asks(api, thread, "Which year?")
    await gives_up_asking(api, thread)
    [row] = await rows(api, project)
    assert (row["group"], row["reason"]) == ("waiting", "question")
    response = await api.client.post(f"/v1/sessions/{thread.id}/messages", json={"content": "2025"}, headers=api.auth())
    assert response.status_code == 202, response.text
    [row] = await rows(api, project)
    assert (row["group"], row["reason"]) == ("working", None)
    await answered(api, thread, "Used 2025.")
    await turn_ends(api, thread)
    [row] = await rows(api, project)
    assert (row["group"], row["status_line"]) == ("idle", "Did the work.")


async def summary_of(api, project: dict) -> dict:
    listed = await api.client.get("/v1/workstreams", headers=api.auth())
    assert listed.status_code == 200, listed.text
    [found] = [summary for summary in listed.json() if summary["id"] == project["id"]]
    got = await api.client.get(f"/v1/workstreams/{project['id']}", headers=api.auth())
    assert {key: got.json()[key] for key in found} == found
    return found


async def test_a_project_counts_its_threads_waiting_on_the_user_and_working(api):
    project = await create(api)
    assert (project["waiting"], project["working"]) == (0, 0)
    await threads_in_every_state(api, await master_of(api, project))
    summary = await summary_of(api, project)
    assert (summary["waiting"], summary["working"]) == (4, 2)


async def test_an_open_question_in_the_master_counts_as_waiting(api):
    project = await create(api)
    master = await master_of(api, project)
    store = api.app.state.session_store
    # A turn's end in the master is news, not a question.
    await store.emit_event(master.id, EventType.INBOX_TASK_COMPLETE, {
        "outcome": "success", "summary": "Started a thread.", "duration_seconds": 1, "session_title": "Quarterly report",
    })
    assert (await summary_of(api, project))["waiting"] == 0
    await asks(api, master, "Which quarter?")
    assert (await summary_of(api, project))["waiting"] == 1


async def test_a_project_is_as_recent_as_its_latest_activity(api):
    budget = await create(api, name="Budget")
    thread = await start(api, await master_of(api, budget))
    hiring = await create(api, name="Hiring")
    listed = (await api.client.get("/v1/workstreams", headers=api.auth())).json()
    assert [summary["id"] for summary in listed][:2] == [hiring["id"], budget["id"]]
    # Work in a thread is the project's activity.
    await answered(api, thread, "Drafted the memo.")
    listed = (await api.client.get("/v1/workstreams", headers=api.auth())).json()
    assert [summary["id"] for summary in listed][:2] == [budget["id"], hiring["id"]]
    [row] = await rows(api, budget)
    assert listed[0]["updated_at"] == row["updated_at"]


async def test_the_list_answers_no_more_projects_than_the_shell_takes(api, monkeypatch):
    monkeypatch.setitem(SHELL_LIMITS, "rows", 2)
    for name in ("Budget", "Hiring", "Audit"):
        await create(api, name=name)
    listed = await api.client.get("/v1/workstreams", headers=api.auth())
    assert [summary["name"] for summary in listed.json()] == ["Audit", "Hiring"]
