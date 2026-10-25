"""Keel reads a step session through ``keel-link``: its events, by a page after a cursor and by a stream
that follows the step live, and its progress.

A page leaves ``llm.delta`` out.  The stream frames each event as the
public session stream does, and follows the step across its turns: only an
archived or deleted session, or its time running out, ends it.  Progress
gives the step's todo list, as its agent last wrote it, and its last
activity, the newest event that is not an ``llm.delta``.

ASGITransport hands a streamed response back only once its generator ends,
so every stream test cuts the stream short, and writes what arrives live
from a task that runs while the stream is open.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

import surogates.api.routes.events as events_module
from surogates.channels.constants import API_CHANNEL
from surogates.session.events import EventType
from surogates.session.provisioning import create_child_session

from .test_events_stream import _read_sse_events
from .test_keel_step_sessions import (  # noqa: F401  (link, link_app and org are fixtures)
    LINK_KEY,
    code_of,
    completed_step,
    link,
    link_app,
    link_headers,
    org,
    page_of,
    progress_of,
    send,
    step_route,
    stream_refused,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: The largest event id Postgres holds: a cursor past it is no cursor.
BIGINT_MAX = 2**63 - 1

REQUEST = (EventType.LLM_REQUEST, {"model": "test-model", "iteration": 1})
RESPONSE = (
    EventType.LLM_RESPONSE,
    {"message": {"role": "assistant", "content": "I read the offer.", "tool_calls": []}},
)
CALL = (
    EventType.TOOL_CALL,
    {"tool_call_id": "call-1", "name": "read_file", "arguments": {"path": "offer.pdf"}},
)
RESULT = (
    EventType.TOOL_RESULT,
    {"tool_call_id": "call-1", "name": "read_file", "content": "The offer, page 1.", "elapsed_ms": 12},
)


def delta(text: str) -> tuple[EventType, dict]:
    return EventType.LLM_DELTA, {"content": text}


async def written(store, session_id, *events: tuple[EventType, dict]) -> list[int]:
    """Write *events* to the session's log, in order: their ids."""
    return [await store.emit_event(session_id, event_type, data) for event_type, data in events]


def short_streams(monkeypatch, duration: float = 3) -> None:
    """Streams that end after *duration* seconds without an event, with a keepalive every 0.2 s."""
    monkeypatch.setattr(events_module, "_MAX_STREAM_DURATION", duration)
    monkeypatch.setattr(events_module, "_KEEPALIVE_INTERVAL", 0.2)


async def watched(
    link,
    session_id,
    *,
    after: int = 0,
    writes=None,
    until: str = "stream.timeout",
    ids: list[str | None] | None = None,
    comments: list[str] | None = None,
) -> list[tuple[str, str]]:
    """What the step's stream from *after* sends, read as ``_read_sse_events`` reads it, while *writes* runs beside it.

    Bounded as a whole at 10 s: a stream that never ended would hang the run rather than fail it.
    """

    async def _read():
        async with link.stream(
            "GET", step_route(session_id, "events/stream"), params={"after": after}, headers=link_headers(LINK_KEY),
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            return await _read_sse_events(response, until_types={until}, deadline_s=8.0, ids=ids, comments=comments)

    writer = asyncio.create_task(writes()) if writes is not None else None
    try:
        return await asyncio.wait_for(_read(), 10)
    finally:
        if writer is not None:
            await writer


# ---------------------------------------------------------------------------
# By page
# ---------------------------------------------------------------------------


async def test_a_page_holds_the_events_after_the_cursor_without_deltas(link, org, session_store, redis_client):
    step = await completed_step(link, org, session_store, redis_client)
    request_id, *_rest = await written(
        session_store, step.id, REQUEST, delta("I read"), delta(" the"), delta(" offer."), RESPONSE, CALL, RESULT,
    )

    whole = await page_of(link, step.id)
    later = await page_of(link, step.id, after=request_id)

    assert whole.status_code == 200, whole.text
    events = whole.json()["events"]
    assert [event["type"] for event in events] == [
        "user.message", "llm.request", "llm.response", "tool.call", "tool.result",
    ]
    ids = [event["id"] for event in events]
    assert ids == sorted(set(ids))
    assert all(set(event) == {"id", "type", "data", "created_at"} for event in events)
    assert events[0]["data"]["synthetic"] == "keel_step"
    assert [event["data"] for event in events[3:]] == [CALL[1], RESULT[1]]
    assert whole.json()["has_more"] is False
    assert later.status_code == 200, later.text
    assert [event["type"] for event in later.json()["events"]] == ["llm.response", "tool.call", "tool.result"]
    assert later.json()["events"] == events[2:]


async def test_each_event_says_when_it_was_written_in_utc(link, org, session_store, redis_client):
    step = await completed_step(link, org, session_store, redis_client)
    kickoff = (await session_store.get_events(step.id))[0]

    answer = await page_of(link, step.id)

    created_at = answer.json()["events"][0]["created_at"]
    assert created_at.endswith("+00:00")
    assert datetime.fromisoformat(created_at).utcoffset() == timedelta(0)
    assert datetime.fromisoformat(created_at) == kickoff.created_at.replace(tzinfo=UTC)


async def test_pages_say_when_more_events_follow(link, org, session_store, redis_client):
    step = await completed_step(link, org, session_store, redis_client)
    await written(
        session_store, step.id, REQUEST, delta("I"), delta(" read"), RESPONSE, REQUEST, delta("Next"),
    )

    first = await page_of(link, step.id, limit=2)
    second_id = first.json()["events"][1]["id"]
    rest = await page_of(link, step.id, after=second_id, limit=2)

    assert first.status_code == 200, first.text
    assert [event["type"] for event in first.json()["events"]] == ["user.message", "llm.request"]
    assert first.json()["has_more"] is True
    # The delta after the last request is no event a page holds: nothing more follows.
    assert [event["type"] for event in rest.json()["events"]] == ["llm.response", "llm.request"]
    assert rest.json()["has_more"] is False


async def test_a_bad_cursor_or_limit_is_invalid(link, org, session_store, redis_client):
    step = await completed_step(link, org, session_store, redis_client)

    answers = [
        await page_of(link, step.id, after=-1),
        await page_of(link, step.id, after="x"),
        await page_of(link, step.id, after="+1"),
        await page_of(link, step.id, after=BIGINT_MAX + 1),
        await page_of(link, step.id, limit=0),
        await page_of(link, step.id, limit=501),
        await stream_refused(link, step.id, after=-1),
        await stream_refused(link, step.id, after=BIGINT_MAX + 1),
    ]
    # The bounds themselves are a cursor and a size.
    newest = await page_of(link, step.id, after=BIGINT_MAX)
    widest = await page_of(link, step.id, limit=500)

    assert [(a.status_code, code_of(a)) for a in answers] == [(422, "invalid-request")] * 8
    assert newest.json() == {"events": [], "has_more": False}
    assert [event["type"] for event in widest.json()["events"]] == ["user.message"]


@pytest.mark.parametrize(
    ("route", "read"),
    [
        ("events", "get_session"),
        ("events", "get_events"),
        ("events/stream", "get_session"),
        ("progress", "get_session"),
        ("progress", "last_event"),
        ("progress", "newest_event"),
    ],
)
async def test_a_failed_read_is_unavailable_and_logs_no_case_data(
    link, link_app, org, session_store, redis_client, monkeypatch, caplog, route, read,
):
    step = await completed_step(link, org, session_store, redis_client)

    # The database's error quotes the rows it read: the step's inputs among them.
    async def failed_read(*args, **kwargs):
        quoted = json.dumps(step.config["case_step"])
        raise OperationalError(
            "SELECT sessions.config FROM sessions", (quoted,),
            Exception(f"canceling statement due to statement timeout\nCONTEXT:  {quoted}"),
        )

    monkeypatch.setattr(link_app.state.session_store, read, failed_read)
    caplog.set_level(logging.INFO)

    failed = await link.get(step_route(step.id, route), headers=link_headers(LINK_KEY))

    assert (failed.status_code, code_of(failed)) == (503, "unavailable")
    assert "OperationalError" in caplog.text
    for value in ("Ioana", "182000.00"):
        assert value not in caplog.text
        assert value not in failed.text


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------
# The step's stream is the public stream's watch, subscribed by pattern. These
# tests follow the pattern of
# test_events_stream.py::test_a_watch_reads_the_chat_once_redis_has_taken_its_subscription:
# they write to the step's log while its watch is open, and read what reaches
# the stream live.


async def test_the_stream_replays_then_goes_live(link, link_app, org, session_store, redis_client, monkeypatch):
    short_streams(monkeypatch)
    step = await completed_step(link, org, session_store, redis_client)
    store = link_app.state.session_store
    kickoff = (await store.get_events(step.id))[0]
    _delta_id, response_id = await written(store, step.id, delta("I read the offer."), RESPONSE)
    live: list[int] = []

    async def now():
        await asyncio.sleep(1.0)
        live.extend(await written(store, step.id, delta("Next,"), CALL))

    ids: list[str | None] = []
    comments: list[str] = []
    events = await watched(link, step.id, writes=now, ids=ids, comments=comments)

    assert [event_type for event_type, _data in events] == [
        "stream.start", "user.message", "llm.response", "stream.live", "llm.delta", "tool.call", "stream.timeout",
    ]
    assert ids == ["0", str(kickoff.id), str(response_id), None, str(live[0]), str(live[1]), None]
    assert comments[0] == "connected"
    assert events[0][1] == events[3][1] == "{}"
    assert json.loads(events[1][1]) == kickoff.data
    assert json.loads(events[1][1])["synthetic"] == "keel_step"
    assert json.loads(events[4][1]) == {"content": "Next,"}
    assert json.loads(events[5][1]) == CALL[1]
    assert events[6] == ("stream.timeout", '{"reason": "max_duration_exceeded"}')


async def test_the_stream_follows_a_step_across_its_turns(
    link, link_app, org, session_store, redis_client, monkeypatch,
):
    short_streams(monkeypatch)
    step = await completed_step(link, org, session_store, redis_client)

    async def answer():
        # Past the grace window in which a stream that does not watch ends on a completed session.
        await asyncio.sleep(1.0)
        answered = await send(link, step.id)
        assert answered.status_code == 202, answered.text

    events = await watched(link, step.id, writes=answer)

    # The step rests completed, and its stream waits for the next turn, which Keel's answer wakes.
    assert [(event_type, json.loads(data).get("synthetic")) for event_type, data in events] == [
        ("stream.start", None),
        ("user.message", "keel_step"),
        ("stream.live", None),
        ("session.resume", None),
        ("user.message", "keel_answer"),
        ("stream.timeout", None),
    ]


async def test_a_stream_reopened_from_its_last_id_gets_every_event_once(
    link, link_app, org, session_store, redis_client, monkeypatch,
):
    short_streams(monkeypatch, duration=1)
    step = await completed_step(link, org, session_store, redis_client)
    store = link_app.state.session_store
    kickoff = (await store.get_events(step.id))[0]
    during: list[int] = []

    async def meanwhile():
        await asyncio.sleep(0.3)
        during.extend(await written(store, step.id, REQUEST))

    first_ids: list[str | None] = []
    first = await watched(link, step.id, writes=meanwhile, ids=first_ids)
    last_id = [event_id for event_id in first_ids if event_id is not None][-1]
    two = await written(store, step.id, RESPONSE, CALL)
    second_ids: list[str | None] = []
    second = await watched(link, step.id, after=int(last_id), ids=second_ids)

    assert first[-1] == ("stream.timeout", '{"reason": "max_duration_exceeded"}')
    assert last_id == str(during[0])
    # The second stream starts at the cursor it was given, and replays exactly the two written since.
    assert second_ids[0] == last_id
    types = [event_type for event_type, _data in second]
    live_at = types.index("stream.live")
    assert list(zip(types[1:live_at], second_ids[1:live_at])) == [
        ("llm.response", str(two[0])), ("tool.call", str(two[1])),
    ]
    # stream.start's id is a cursor, not an event: every event's id comes once across the two streams.
    event_ids = [
        int(event_id)
        for (event_type, _data), event_id in zip(first + second, first_ids + second_ids)
        if event_type != "stream.start" and event_id is not None
    ]
    assert event_ids == [kickoff.id, during[0], *two]


async def test_the_stream_ends_when_the_session_is_archived(
    link, link_app, org, session_store, redis_client, monkeypatch,
):
    short_streams(monkeypatch)
    step = await completed_step(link, org, session_store, redis_client)

    async def archive():
        await asyncio.sleep(0.5)
        await link_app.state.session_store.update_session_status(step.id, "archived")

    events = await watched(link, step.id, writes=archive, until="session.done")

    assert events[-1] == ("session.done", '{"reason": "archived", "status": "archived"}')
    assert "stream.timeout" not in [event_type for event_type, _data in events]


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------

TODOS = [
    {"id": "1", "content": "Read the payslips", "status": "completed"},
    {"id": "2", "content": "Check the employer", "status": "in_progress"},
]


def todos(items: list) -> tuple[EventType, dict]:
    return EventType.TODO_UPDATED, {"todos": items}


async def written_at(store, session_id, event_id: int) -> datetime:
    """When the session's event *event_id* was written, in UTC."""
    [event] = [event for event in await store.get_events(session_id) if event.id == event_id]
    return event.created_at.replace(tzinfo=UTC)


def in_utc(value: str) -> datetime:
    """*value*, an ISO 8601 time that must carry the offset of UTC."""
    assert value.endswith("+00:00"), value
    parsed = datetime.fromisoformat(value)
    assert parsed.utcoffset() == timedelta(0)
    return parsed


async def test_progress_gives_the_todo_list_and_the_last_activity(link, org, session_store, redis_client):
    step = await completed_step(link, org, session_store, redis_client)
    todo_id, call_id, _delta_id = await written(session_store, step.id, todos(TODOS), CALL, delta("Reading"))

    answer = await progress_of(link, step.id)

    assert answer.status_code == 200, answer.text
    progress = answer.json()
    assert set(progress) == {"status", "todos", "todos_at", "last_activity"}
    assert progress["status"] == "completed"
    assert progress["todos"] == TODOS
    assert in_utc(progress["todos_at"]) == await written_at(session_store, step.id, todo_id)
    # The delta after the call is a piece of a response, no activity of its own.
    activity = progress["last_activity"]
    assert {key: activity[key] for key in ("event_id", "type", "tool")} == {
        "event_id": call_id, "type": "tool.call", "tool": "read_file",
    }
    assert set(activity) == {"event_id", "type", "at", "tool"}
    assert in_utc(activity["at"]) == await written_at(session_store, step.id, call_id)

    [result_id] = await written(session_store, step.id, RESULT)
    after_result = (await progress_of(link, step.id)).json()["last_activity"]

    assert (after_result["event_id"], after_result["type"], after_result["tool"]) == (
        result_id, "tool.result", "read_file",
    )


async def test_progress_tells_a_list_never_written_from_an_emptied_one(link, org, session_store, redis_client):
    step = await completed_step(link, org, session_store, redis_client)
    kickoff = (await session_store.get_events(step.id))[0]

    never = (await progress_of(link, step.id)).json()
    [emptied_id] = await written(session_store, step.id, todos([]))
    emptied = (await progress_of(link, step.id)).json()

    assert (never["todos"], never["todos_at"]) == (None, None)
    assert never["last_activity"] == {
        "event_id": kickoff.id,
        "type": "user.message",
        "at": kickoff.created_at.replace(tzinfo=UTC).isoformat(),
        "tool": None,
    }
    assert emptied["todos"] == []
    assert in_utc(emptied["todos_at"]) == await written_at(session_store, step.id, emptied_id)
    assert (emptied["last_activity"]["event_id"], emptied["last_activity"]["tool"]) == (emptied_id, None)


async def test_progress_reads_only_the_steps_own_session(link, org, session_store, redis_client):
    step = await completed_step(link, org, session_store, redis_client)
    kickoff = (await session_store.get_events(step.id))[0]
    # A helper the step started keeps its own list.
    helper = await create_child_session(store=session_store, parent=step, channel=API_CHANNEL)
    await written(session_store, helper.id, todos(TODOS), CALL)

    progress = (await progress_of(link, step.id)).json()

    assert (progress["todos"], progress["todos_at"]) == (None, None)
    assert progress["last_activity"]["event_id"] == kickoff.id


#: A stored value out of its shape, quoting the case: an error's text would quote it too.
CASE_TEXT = "Check Ioana's employer"


async def overwritten(session_factory, event_id: int, data) -> None:
    """Store *data* as the event's payload, past the checks every writer makes: a row out of its shape."""
    async with session_factory() as db:
        await db.execute(
            text("UPDATE events SET data = CAST(:data AS jsonb) WHERE id = :id"),
            {"data": json.dumps(data), "id": event_id},
        )
        await db.commit()


def route_log(caplog) -> str:
    """What keel-link's routes logged, without the client's own request lines, which name the session too."""
    return "\n".join(record.getMessage() for record in caplog.records if record.name == "surogates.api.routes.keel")


def no_case_text(answer, caplog) -> None:
    assert CASE_TEXT not in answer.text
    assert "Ioana" not in caplog.text


async def test_progress_leaves_out_a_todo_item_out_of_its_shape(link, org, session_store, redis_client, caplog):
    step = await completed_step(link, org, session_store, redis_client)
    [todo_id] = await written(
        session_store, step.id, todos([TODOS[0], {"id": 1, "content": CASE_TEXT, "status": "pending"}, TODOS[1]]),
    )
    caplog.set_level(logging.INFO)
    # Only what the read logs.
    caplog.clear()

    answer = await progress_of(link, step.id)

    assert answer.status_code == 200, answer.text
    assert answer.json()["todos"] == TODOS
    assert in_utc(answer.json()["todos_at"]) == await written_at(session_store, step.id, todo_id)
    assert str(step.id) in route_log(caplog) and "ValidationError" in route_log(caplog)
    no_case_text(answer, caplog)


@pytest.mark.parametrize("stored", ["todos-not-a-list", "payload-not-an-object"])
async def test_progress_reads_a_todo_payload_out_of_its_shape_as_no_list(
    link, org, session_store, session_factory, redis_client, caplog, stored,
):
    step = await completed_step(link, org, session_store, redis_client)
    if stored == "todos-not-a-list":
        await written(session_store, step.id, (EventType.TODO_UPDATED, {"todos": {"1": CASE_TEXT}}))
    else:
        [todo_id] = await written(session_store, step.id, todos(TODOS))
        await overwritten(session_factory, todo_id, [CASE_TEXT])
    caplog.set_level(logging.INFO)
    # Only what the read logs.
    caplog.clear()

    answer = await progress_of(link, step.id)

    assert answer.status_code == 200, answer.text
    assert (answer.json()["todos"], answer.json()["todos_at"]) == (None, None)
    assert str(step.id) in route_log(caplog)
    no_case_text(answer, caplog)


async def test_progress_reads_a_malformed_newest_event_as_no_activity(
    link, org, session_store, session_factory, redis_client, caplog,
):
    step = await completed_step(link, org, session_store, redis_client)
    _todo_id, call_id = await written(session_store, step.id, todos(TODOS), CALL)
    await overwritten(session_factory, call_id, [CASE_TEXT])
    caplog.set_level(logging.INFO)
    # Only what the read logs.
    caplog.clear()

    answer = await progress_of(link, step.id)

    assert answer.status_code == 200, answer.text
    assert answer.json()["last_activity"] is None
    assert answer.json()["todos"] == TODOS
    assert str(step.id) in route_log(caplog) and "ValidationError" in route_log(caplog)
    no_case_text(answer, caplog)
