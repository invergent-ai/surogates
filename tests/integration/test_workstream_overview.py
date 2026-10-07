"""A project's Overview over the API: its threads' rows, its counts, resolving
and reopening, its stream, and starting a proposed thread from its card."""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.responses import StreamingResponse
from sqlalchemy import select

import surogates.api.routes.workstreams as workstreams_routes
from surogates.db.models import InboxItem
from surogates.session.events import EventType
from surogates.session.provisioning import create_child_session
from surogates.session.store import SessionStore
from surogates.tenant.auth.jwt import create_service_account_session_token
from surogates.workstreams import stream as project_stream
from surogates.workstreams.derive import SHELL_LIMITS, derive_thread
from surogates.workstreams.store import WorkstreamStore
from tests.test_steer_loop import _final_response, _make_loop_harness

from . import test_workstream_threads as threads_tests
from .conftest import issue_service_account_token
from .test_devices import add_user, api, next_control  # noqa: F401  (api is a fixture)
from .test_workstream_threads import (
    PROPOSED,
    TODO_CALL,
    answered,
    asks,
    call_tool,
    events_of,
    gives_up_asking,
    live_turn,
    queued,
    quiet_for,
    resolve,
    start,
    threads_in_every_state,
    turn_ends,
)
from .test_workstreams import create, master_of, system_prompt

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


def publishing(api, monkeypatch) -> None:
    """The app's store, publishing as production's does: the fixture's has no Redis."""
    state = api.app.state
    monkeypatch.setattr(state, "session_store", SessionStore(state.session_factory, state.redis))


def heard(api, monkeypatch) -> list[tuple[str, str, dict[str, str]]]:
    """Each change published on a project's stream: the session, the kind, and
    every thread's group as a client refetching at that moment reads it."""
    publishing(api, monkeypatch)
    changes: list[tuple[str, str, dict[str, str]]] = []
    publish = project_stream.publish

    async def recorded(redis, workstream_id, session_id, kind):
        found = await WorkstreamStore(api.app.state.session_factory).thread_facts(UUID(str(workstream_id)))
        now = datetime.now(timezone.utc)
        changes.append((str(session_id), kind, {str(f.id): derive_thread(f, now=now)["group"] for f in found}))
        await publish(redis, workstream_id, session_id, kind)

    monkeypatch.setattr(project_stream, "publish", recorded)
    return changes


async def streamed(api, monkeypatch, project: dict, changes: int, act) -> list[tuple[str, dict]]:
    """What the project's stream sends while *act* runs, up to its *changes*-th
    change: the test client reads a response whole, so it must end."""
    publishing(api, monkeypatch)

    def respond(events):
        async def ending():
            seen = 0
            async with contextlib.aclosing(events) as sent:
                async for event in sent:
                    yield f"event: {event['event']}\ndata: {event['data']}\n\n"
                    seen += event["event"] == "change"
                    if seen == changes:
                        return

        return StreamingResponse(ending(), media_type="text/event-stream")

    monkeypatch.setattr(workstreams_routes, "EventSourceResponse", respond)
    channel = f"surogates:workstream:{project['id']}"

    async def acting():
        while (await api.app.state.redis.pubsub_numsub(channel))[0][1] == 0:
            await asyncio.sleep(0.01)
        await act()

    actor = asyncio.create_task(acting())
    try:
        async with asyncio.timeout(10):
            response = await api.client.get(f"/v1/workstreams/{project['id']}/stream", headers=api.auth())
            assert response.status_code == 200, response.text
            await actor
    finally:
        actor.cancel()
    return [
        (event.removeprefix("event: "), json.loads(data.removeprefix("data: ")))
        for event, data in (block.split("\n") for block in response.text.strip().split("\n\n"))
    ]


async def test_a_threads_question_and_its_turns_end_reach_the_projects_stream(api, monkeypatch):
    project = await create(api)
    thread = await start(api, await master_of(api, project))

    async def act():
        await api.app.state.session_store.emit_event(
            thread.id, EventType.ITERATION_SUMMARY, {"summary": "Reading the brief"},
        )
        typed = await api.client.post(
            f"/v1/sessions/{thread.id}/messages", json={"content": "Keep it to a page."}, headers=api.auth(),
        )
        assert typed.status_code == 202, typed.text
        await asks(api, thread, "Which year?")
        await answered(api, thread, "Drafted the memo.")
        await turn_ends(api, thread)

    sent = await streamed(api, monkeypatch, project, 6, act)
    tid = str(thread.id)
    assert sent == [
        ("ready", {}),
        ("change", {"thread_id": tid, "type": "iteration.summary"}),
        ("change", {"thread_id": tid, "type": "user.message"}),
        ("change", {"thread_id": tid, "type": "inbox.input_required"}),
        ("change", {"thread_id": tid, "type": "turn.summary"}),
        ("change", {"thread_id": tid, "type": "session.complete"}),
        # The master's, after the thread's status: the change a client hears last.
        ("change", {"thread_id": None, "type": "worker.complete"}),
    ]


async def test_the_last_change_of_a_turn_reads_its_end(api, monkeypatch):
    project = await create(api)
    thread = await start(api, await master_of(api, project))
    changes = heard(api, monkeypatch)
    await answered(api, thread, "Drafted the memo.")
    await turn_ends(api, thread)
    *_, (_, kind, groups) = changes
    assert (kind, groups[str(thread.id)]) == ("worker.complete", "idle")


async def test_an_answer_on_the_questions_card_is_heard_once_its_wait_is_over(api, monkeypatch):
    project = await create(api)
    thread = await start(api, await master_of(api, project))
    call_id = await asks(api, thread, "Which year?")
    changes = heard(api, monkeypatch)
    response = await api.client.post(
        f"/v1/sessions/{thread.id}/ask_user_question/{call_id}/respond",
        json={"responses": [{"question": "Which year?", "answer": "2025"}]}, headers=api.auth(),
    )
    assert response.status_code == 201, response.text
    *_, (session_id, kind, groups) = changes
    assert (session_id, kind, groups[str(thread.id)]) == (str(thread.id), "ask_user_question.response", "working")


async def test_an_approval_answered_in_the_inbox_is_heard_once_its_wait_is_over(api, monkeypatch):
    project = await create(api)
    thread = await start(api, await master_of(api, project))
    await api.app.state.session_store.emit_event(thread.id, EventType.INBOX_ACTION_REQUIRED, {
        "title": "Send the draft to finance?", "action_type": "approval",
    })
    async with api.app.state.session_factory() as db:
        item_id = await db.scalar(select(InboxItem.id).where(InboxItem.session_id == thread.id))
    changes = heard(api, monkeypatch)
    response = await api.client.post(f"/v1/inbox/{item_id}/respond", json={"completed": True}, headers=api.auth())
    assert response.status_code == 200, response.text
    *_, (session_id, kind, groups) = changes
    assert (session_id, kind, groups[str(thread.id)]) == (str(thread.id), "user.message", "working")


async def test_a_masters_own_work_is_heard_only_where_it_changes_a_count_or_a_card(api, monkeypatch):
    project = await create(api)
    master = await master_of(api, project)
    changes = heard(api, monkeypatch)
    store = api.app.state.session_store
    for kind, data in (
        (EventType.ITERATION_SUMMARY, {"summary": "Reading the brief"}),
        (EventType.TODO_UPDATED, {"todos": []}),
        (EventType.HARNESS_WAKE, {}),
    ):
        await store.emit_event(master.id, kind, data)
    await asks(api, master, "Which quarter?")
    assert [kind for _, kind, _ in changes] == ["inbox.input_required"]


async def test_a_delegated_childs_approval_reaches_its_projects_stream(api, monkeypatch):
    project = await create(api)
    thread = await start(api, await master_of(api, project))
    child = await create_child_session(store=api.app.state.session_store, parent=thread, channel="delegation")

    async def act():
        await api.app.state.session_store.emit_event(child.id, EventType.INBOX_ACTION_REQUIRED, {
            "title": "Open the bank's site?", "action_type": "approval",
        })

    # A session under a thread is no row of its own: the client refetches the project's.
    assert await streamed(api, monkeypatch, project, 1, act) == [
        ("ready", {}), ("change", {"thread_id": None, "type": "inbox.action_required"}),
    ]
    [row] = await rows(api, project)
    assert (row["group"], row["reason"]) == ("waiting", "approval")


async def test_a_chat_outside_projects_publishes_on_no_projects_stream(api, monkeypatch):
    publishing(api, monkeypatch)
    chat = await api.client.post("/v1/sessions", json={}, headers=api.auth())
    listener = api.app.state.redis.pubsub()
    await listener.psubscribe("surogates:workstream:*")
    try:
        await api.app.state.session_store.emit_event(UUID(chat.json()["id"]), EventType.SESSION_COMPLETE, {})
        assert await listener.get_message(ignore_subscribe_messages=True, timeout=0.5) is None
    finally:
        await listener.aclose()


async def test_only_the_owner_hears_a_projects_stream_and_only_over_redis(api, session_factory, monkeypatch):
    project = await create(api)
    _, their_token = await add_user(session_factory, api.org_id)
    response = await api.client.get(f"/v1/workstreams/{project['id']}/stream", headers=api.auth(their_token))
    assert response.status_code == 404, response.text
    monkeypatch.setattr(api.app.state, "redis", None)
    response = await api.client.get(f"/v1/workstreams/{project['id']}/stream", headers=api.auth())
    assert response.status_code == 503, response.text


async def act_on(api, project: dict, thread, action: str, token: str | None = None):
    return await api.client.post(
        f"/v1/workstreams/{project['id']}/threads/{thread.id}/{action}", headers=api.auth(token),
    )


async def test_the_user_resolves_a_working_thread_and_it_stops(api):
    project = await create(api)
    master = await master_of(api, project)
    thread = await start(api, master)
    before = len(await events_of(api, master.id))
    listener = api.app.state.redis.pubsub()
    await listener.subscribe(f"surogates:interrupt:{thread.id}")
    try:
        response = await act_on(api, project, thread, "resolve")
        assert response.status_code == 200, response.text
        assert json.loads(await next_control(listener)) == {"reason": "resolved by the user"}
    finally:
        await listener.aclose()
    row = response.json()
    assert (row["id"], row["group"]) == (str(thread.id), "resolved")
    assert row["resolved_at"].endswith("Z")
    assert (await api.app.state.session_store.get_session(thread.id)).status == "paused"
    [paused] = await events_of(api, thread.id, EventType.SESSION_PAUSE)
    assert paused.data == {"reason": "resolved by the user"}
    # Nothing that only the Overview needs is written into the master's log.
    assert len(await events_of(api, master.id)) == before


async def test_reopening_takes_a_thread_out_of_resolved(api):
    project = await create(api)
    master = await master_of(api, project)
    resolved, quiet = await start(api, master, title="Draft A"), await start(api, master, title="Draft B", goal="B.")
    for thread in (resolved, quiet):
        await answered(api, thread, "Drafted it.")
        await turn_ends(api, thread)
    assert (await act_on(api, project, resolved, "resolve")).json()["group"] == "resolved"
    await quiet_for(api, quiet, days=8)
    assert [row["group"] for row in await rows(api, project, thread_id=str(quiet.id))] == ["resolved"]
    for thread in (resolved, quiet):
        response = await act_on(api, project, thread, "reopen")
        assert response.status_code == 200, response.text
        assert (response.json()["group"], response.json()["resolved_at"]) == ("idle", None)


async def test_a_message_typed_into_a_resolved_thread_reopens_it(api):
    project = await create(api)
    thread = await start(api, await master_of(api, project))
    await answered(api, thread, "Drafted the memo.")
    await turn_ends(api, thread)
    await resolve(api, thread)
    response = await api.client.post(
        f"/v1/sessions/{thread.id}/messages", json={"content": "Add a chart."}, headers=api.auth(),
    )
    assert response.status_code == 202, response.text
    [row] = await rows(api, project)
    assert (row["group"], row["resolved_at"]) == ("working", None)


@pytest.mark.parametrize("route, status", [("resume", "paused"), ("retry", "failed")])
async def test_bringing_a_resolved_thread_back_reopens_it(api, monkeypatch, route, status):
    project = await create(api)
    thread = await start(api, await master_of(api, project))
    await api.app.state.session_store.update_session_status(thread.id, status)
    await resolve(api, thread)
    changes = heard(api, monkeypatch)
    response = await api.client.post(f"/v1/sessions/{thread.id}/{route}", headers=api.auth())
    assert response.status_code == 200, response.text
    [row] = await rows(api, project)
    assert (row["group"], row["resolved_at"]) == ("working", None)
    # Heard as it resumes, the thread already reads working.
    assert [groups[str(thread.id)] for _, kind, groups in changes if kind == "session.resume"] == ["working"]


async def test_a_resolve_reaches_the_projects_stream(api, monkeypatch):
    project = await create(api)
    thread = await start(api, await master_of(api, project))

    async def act():
        await act_on(api, project, thread, "resolve")

    tid = str(thread.id)
    assert await streamed(api, monkeypatch, project, 2, act) == [
        ("ready", {}),
        ("change", {"thread_id": tid, "type": "session.pause"}),
        ("change", {"thread_id": tid, "type": "thread.resolved"}),
    ]


async def test_only_the_projects_live_threads_are_resolved_or_reopened(api, session_factory):
    project = await create(api)
    master = await master_of(api, project)
    deleted = await start(api, master, title="Draft A")
    await api.app.state.session_store.update_session_status(deleted.id, "archived")
    others = await start(api, await master_of(api, await create(api, name="Budget")))
    _, their_token = await add_user(session_factory, api.org_id)
    for action in ("resolve", "reopen"):
        for thread in (deleted, others):
            assert (await act_on(api, project, thread, action)).status_code == 404
        mine = await start(api, master, title="Draft B", goal="B.")
        assert (await act_on(api, project, mine, action, their_token)).status_code == 404


async def proposed(api, master) -> str:
    """The master proposes ``PROPOSED``: a thread in the cloud, keyed "1", and one on the user's computer, "2"."""
    return (await call_tool(api, master, "propose_threads", threads=PROPOSED))["proposal_id"]


async def start_card(api, project: dict, proposal_id: str, key: str, token: str | None = None):
    return await api.client.post(
        f"/v1/workstreams/{project['id']}/threads", json={"proposal_id": proposal_id, "key": key},
        headers=api.auth(token),
    )


async def test_the_user_starts_a_proposed_thread_from_its_card(api):
    project = await create(api)
    master = await master_of(api, project)
    proposal_id = await proposed(api, master)
    response = await start_card(api, project, proposal_id, "1")
    assert response.status_code == 201, response.text
    row = response.json()
    assert (row["title"], row["group"]) == ("Draft A", "working")
    thread = await api.app.state.session_store.get_session(UUID(row["id"]))
    assert thread.parent_id == master.id
    [goal] = await events_of(api, thread.id, EventType.USER_MESSAGE)
    assert goal.data == {"content": "Draft the A memo as A.docx."}
    [spawned] = await events_of(api, master.id, EventType.WORKER_SPAWNED)
    assert spawned.data == {
        "worker_id": row["id"], "title": "Draft A", "goal": "Draft the A memo as A.docx.",
        "started_by": "user", "proposal_id": proposal_id, "key": "1",
    }
    assert await queued(api, thread)


async def test_a_card_started_twice_starts_one_thread(api):
    project = await create(api)
    master = await master_of(api, project)
    proposal_id = await proposed(api, master)
    assert (await start_card(api, project, proposal_id, "1")).status_code == 201
    again = await start_card(api, project, proposal_id, "1")
    assert (again.status_code, again.json()["detail"]) == (409, "This thread was already started.")
    # At once, as a double click sends it.
    proposal_id = await proposed(api, master)
    both = await asyncio.gather(*(start_card(api, project, proposal_id, "1") for _ in range(2)))
    assert sorted(response.status_code for response in both) == [201, 409]
    assert len(await events_of(api, master.id, EventType.WORKER_SPAWNED)) == 2


async def test_a_thread_on_the_users_computer_is_not_started_here(api):
    project = await create(api)
    master = await master_of(api, project)
    response = await start_card(api, project, await proposed(api, master), "2")
    assert response.status_code == 409, response.text
    assert await events_of(api, master.id, EventType.WORKER_SPAWNED) == []


async def test_only_a_card_of_the_projects_own_proposals_starts(api, session_factory):
    project = await create(api)
    proposal_id = await proposed(api, await master_of(api, project))
    budget = await create(api, name="Budget")
    theirs = await proposed(api, await master_of(api, budget))
    _, their_token = await add_user(session_factory, api.org_id)
    for case, (proposal, key, token) in {
        "an unknown proposal": (str(uuid4()), "1", None),
        "an unknown card": (proposal_id, "3", None),
        "another project's proposal": (theirs, "1", None),
        "another user": (proposal_id, "1", their_token),
    }.items():
        response = await start_card(api, project, proposal, key, token)
        assert response.status_code == 404, case
    assert (await start_card(api, project, proposal_id, "one")).status_code == 422


@pytest.mark.parametrize("method, path", [
    ("get", "threads"),
    ("get", "stream"),
    ("post", "threads"),
    ("post", "threads/{thread}/resolve"),
    ("post", "threads/{thread}/reopen"),
])
async def test_a_session_token_reaches_no_projects_route(api, session_factory, method, path):
    project = await create(api)
    thread = await start(api, await master_of(api, project))
    chat = await api.client.post("/v1/sessions", json={}, headers=api.auth())
    account = await issue_service_account_token(session_factory, api.org_id)
    token = create_service_account_session_token(api.org_id, account.id, UUID(chat.json()["id"]))
    body = {"json": {"proposal_id": str(uuid4()), "key": "1"}} if (method, path) == ("post", "threads") else {}
    response = await getattr(api.client, method)(
        f"/v1/workstreams/{project['id']}/{path.format(thread=thread.id)}", headers=api.auth(token), **body,
    )
    assert response.status_code == 403, response.text


async def test_a_master_leaves_proposed_threads_to_their_cards(api):
    prompt = await system_prompt(api, await master_of(api, await create(api)))
    assert "A proposed thread is started from its card" in prompt


async def coordinator_with_a_report(api):
    """A chat outside projects whose worker reported after the user's request."""
    chat = await api.client.post("/v1/sessions", json={}, headers=api.auth())
    store = api.app.state.session_store
    parent = await store.get_session(UUID(chat.json()["id"]))
    await store.emit_event(parent.id, EventType.USER_MESSAGE, {"content": "Check the Q3 figures."})
    worker = await create_child_session(store=store, parent=parent, channel="worker")
    await answered(api, worker, "Checked the figures.")
    await turn_ends(api, worker)
    return parent, worker


def harnesses(monkeypatch) -> list:
    """The harness each ``live_turn`` makes, so its turn's end can be read."""
    made = []

    def harness(**options):
        made.append(_make_loop_harness(**options))
        return made[-1]

    monkeypatch.setattr(threads_tests, "_make_loop_harness", harness)
    return made


async def test_a_turns_summary_takes_the_users_request_not_a_report(api, monkeypatch):
    # A coordinator outside projects reads its worker's report after the
    # user's message; the summary is judged against what the user asked.
    parent, worker = await coordinator_with_a_report(api)
    made = harnesses(monkeypatch)
    [request] = await live_turn(api, monkeypatch, parent, [_final_response("The figures add up.")])
    assert request[-1]["content"] == f"[Worker {worker.id} completed]\nChecked the figures."
    assert made[0]._complete_session.await_args.kwargs["user_message"] == "Check the Q3 figures."


async def test_a_turn_steered_after_a_report_is_summed_up_against_the_steer(api, monkeypatch):
    parent, _ = await coordinator_with_a_report(api)
    made = harnesses(monkeypatch)

    async def steered():
        await api.app.state.session_store.emit_event(
            parent.id, EventType.USER_MESSAGE, {"content": "Use the Q4 figures instead."},
        )

    await live_turn(api, monkeypatch, parent, [TODO_CALL, _final_response("Q4 adds up.")], during_tool=steered)
    assert made[0]._complete_session.await_args.kwargs["user_message"] == "Use the Q4 figures instead."


async def test_a_turn_out_of_iterations_is_summed_up_against_the_users_request(api, monkeypatch):
    parent, _ = await coordinator_with_a_report(api)
    made = harnesses(monkeypatch)
    await live_turn(api, monkeypatch, parent, [TODO_CALL, _final_response("So far, Q3 adds up.")], budget=1)
    ended = made[0]._complete_session.await_args.kwargs
    assert (ended["reason"], ended["user_message"]) == ("budget_exhausted", "Check the Q3 figures.")
