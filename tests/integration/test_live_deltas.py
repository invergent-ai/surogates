"""Live llm.delta frames over Redis reach every live reader in order, and no log row is written.

Real Postgres and Redis.  Covers the SSE route (order against the persisted
events, a frame of a call already ended dropped, several viewers, a resume
from a frame's id, no Redis), the OpenAI-compatible streaming route (a reader
that joins mid-reply gets the whole text), the voice turn stream, and the
opt-in cleanup of the rows older releases wrote.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update

from surogates.api.routes.openai import _stream_turn, _TurnReader
from surogates.db.delta_purge import purge_llm_delta_rows
from surogates.db.models import Event as EventRow
from surogates.session.events import EventType
from surogates.session.store import SessionStore
from surogates.tenant.auth.jwt import create_access_token
from surogates.tenant.credentials import CredentialVault
from surogates.voice.sessions import CallSession

from .conftest import create_org, create_user
from .test_events_stream import _read_sse_events

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _app(session_factory, redis_client, pg_url, redis_url):
    os.environ["SUROGATES_DB_URL"] = pg_url
    os.environ["SUROGATES_REDIS_URL"] = redis_url

    from surogates.api.app import create_app
    from surogates.config import Settings
    from surogates.storage.backend import create_backend

    application = create_app()
    application.state.session_factory = session_factory
    application.state.redis = redis_client
    application.state.session_store = SessionStore(session_factory, redis=redis_client)
    application.state.settings = Settings()
    application.state.storage = create_backend(application.state.settings)
    application.state.credential_vault = CredentialVault(session_factory, Fernet.generate_key())
    return application


@pytest_asyncio.fixture(loop_scope="session")
async def live(session_factory, redis_client, pg_url, redis_url):
    """An app with Redis, its client, and an active session with a user token."""
    app = _app(session_factory, redis_client, pg_url, redis_url)
    store: SessionStore = app.state.session_store
    org_id = await create_org(session_factory)
    user_id = uuid.uuid4()
    await create_user(session_factory, org_id, user_id=user_id)
    token = create_access_token(org_id, user_id, {"sessions:read", "sessions:write"})
    session = await store.create_session(user_id=user_id, org_id=org_id, agent_id="test-agent")
    await store.update_session_status(session.id, "active")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=10.0) as client:
        yield app, client, store, session, token


async def _frame(store: SessionStore, session_id, after: int, seq: int, **data) -> None:
    await store.publish_live_frame(session_id, data, after=after, seq=seq)


async def _end_turn(store: SessionStore, session_id) -> int:
    event_id = await store.emit_event(session_id, EventType.SESSION_COMPLETE, {})
    await store.update_session_status(session_id, "completed")
    return event_id


async def _watch(client, session_id, token, *, after=0, last_event_id=None, ids=None, until=("session.done",)):
    headers = {"Authorization": f"Bearer {token}"}
    if last_event_id is not None:
        headers["Last-Event-ID"] = last_event_id
    async with client.stream("GET", f"/v1/sessions/{session_id}/events?after={after}", headers=headers) as response:
        return await _read_sse_events(response, until_types=set(until), deadline_s=8.0, ids=ids)


async def _when_live(store, session_id, reader_task, *, settle=0.4):
    """Give a just-opened stream time to subscribe and send its backlog."""
    await asyncio.sleep(settle)
    assert not reader_task.done()


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


async def test_a_calls_frames_reach_a_viewer_between_its_request_and_its_response(live):
    app, client, store, session, token = live
    ids: list = []
    reader = asyncio.create_task(_watch(client, session.id, token, ids=ids))
    await _when_live(store, session.id, reader)

    request = await store.emit_event(session.id, EventType.LLM_REQUEST, {"iteration": 1})
    await _frame(store, session.id, request, 0, reasoning="Let me think. ")
    await _frame(store, session.id, request, 1, content="Hello, ")
    await _frame(store, session.id, request, 2, content="world.")
    response = await store.emit_event(session.id, EventType.LLM_RESPONSE, {"message": {"content": "Hello, world."}})
    # A frame of the call after its response (a reader that fell behind would see it so): never shown.
    await _frame(store, session.id, request, 3, content=" STALE")
    await _end_turn(store, session.id)
    events = await reader

    kinds = [kind for kind, _ in events if kind != "stream.live"]
    assert kinds == ["llm.request", "llm.delta", "llm.delta", "llm.delta", "llm.response", "session.complete", "session.done"]
    deltas = [json.loads(data) for kind, data in events if kind == "llm.delta"]
    assert "".join(d.get("content", "") for d in deltas) == "Hello, world."
    # A frame's id is minus the last persisted event the stream sent: never an event's own id.
    frame_ids = [i for (kind, _), i in zip(events, ids) if kind == "llm.delta"]
    assert frame_ids == [f"-{request}"] * 3
    assert str(response) in ids
    async with store._sf() as db:  # noqa: SLF001
        rows = (await db.execute(select(EventRow.type).where(EventRow.session_id == session.id))).scalars().all()
    assert "llm.delta" not in rows


async def test_frames_published_faster_than_the_stream_reads_keep_their_order(live):
    """Frames and the events after them queue up while the stream is busy: they go out in publish order."""
    app, client, store, session, token = live
    reader = asyncio.create_task(_watch(client, session.id, token))
    await _when_live(store, session.id, reader)
    for call in range(3):
        request = await store.emit_event(session.id, EventType.LLM_REQUEST, {"iteration": call})
        for seq in range(20):
            await _frame(store, session.id, request, seq, content=f"{call}.{seq} ")
        await store.emit_event(session.id, EventType.LLM_RESPONSE, {"message": {"content": f"call {call}"}})
    await _end_turn(store, session.id)
    events = await reader

    seen = []
    for kind, data in events:
        if kind == "llm.request":
            seen.append("request")
        elif kind == "llm.delta":
            seen.append(json.loads(data)["content"])
        elif kind == "llm.response":
            seen.append("response")
    expected = []
    for c in range(3):
        expected += ["request", *[f"{c}.{s} " for s in range(20)], "response"]
    assert seen == expected


async def test_every_viewer_gets_every_frame(live):
    app, client, store, session, token = live
    readers = [asyncio.create_task(_watch(client, session.id, token)) for _ in range(3)]
    await asyncio.sleep(0.4)
    request = await store.emit_event(session.id, EventType.LLM_REQUEST, {})
    for seq, piece in enumerate(("a", "b", "c")):
        await _frame(store, session.id, request, seq, content=piece)
    await store.emit_event(session.id, EventType.LLM_RESPONSE, {"message": {"content": "abc"}})
    await _end_turn(store, session.id)
    for events in await asyncio.gather(*readers):
        assert "".join(json.loads(d)["content"] for k, d in events if k == "llm.delta") == "abc"


@pytest.mark.parametrize("how", ["header", "query"])
async def test_a_reconnect_from_a_frames_id_resumes_after_the_event_before_it(live, how):
    app, client, store, session, token = live
    await store.emit_event(session.id, EventType.USER_MESSAGE, {"content": "hi"})  # so the request's id is not 1
    request = await store.emit_event(session.id, EventType.LLM_REQUEST, {})
    response = await store.emit_event(session.id, EventType.LLM_RESPONSE, {"message": {"content": "x"}})
    await _end_turn(store, session.id)
    # The client's last frame was a live one, sent after the llm.request: id -<request>.
    if how == "header":
        events = await _watch(client, session.id, token, after=0, last_event_id=f"-{request}")
    else:
        events = await _watch(client, session.id, token, after=-request)
    assert [k for k, _ in events if k not in ("stream.live",)] == ["llm.response", "session.complete", "session.done"]
    assert json.loads(events[0][1])["message"]["content"] == "x" and response > request


async def test_without_redis_the_stream_has_no_frames_and_the_reply_arrives_whole(session_factory, pg_url, redis_url):
    app = _app(session_factory, None, pg_url, redis_url)
    store: SessionStore = app.state.session_store
    org_id = await create_org(session_factory)
    user_id = uuid.uuid4()
    await create_user(session_factory, org_id, user_id=user_id)
    token = create_access_token(org_id, user_id, {"sessions:read", "sessions:write"})
    session = await store.create_session(user_id=user_id, org_id=org_id, agent_id="test-agent")
    await store.update_session_status(session.id, "active")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=10.0) as client:
        reader = asyncio.create_task(_watch(client, session.id, token))
        await asyncio.sleep(0.3)
        request = await store.emit_event(session.id, EventType.LLM_REQUEST, {})
        await _frame(store, session.id, request, 0, content="lost")  # no Redis: goes nowhere
        await store.emit_event(session.id, EventType.LLM_RESPONSE, {"message": {"content": "whole"}})
        await _end_turn(store, session.id)
        events = await reader
    assert "llm.delta" not in [k for k, _ in events]
    assert json.loads(next(d for k, d in events if k == "llm.response"))["message"]["content"] == "whole"


async def test_a_reader_behind_the_channel_sends_every_event_before_the_end(live, monkeypatch):
    """A busy api reads the log late: the turn's last events still go out before session.done."""
    app, client, store, session, token = live
    slow_store = app.state.session_store
    real = slow_store.get_events

    async def slow_get_events(*args, **kwargs):
        await asyncio.sleep(0.15)
        return await real(*args, **kwargs)

    monkeypatch.setattr(slow_store, "get_events", slow_get_events)
    reader = asyncio.create_task(_watch(client, session.id, token))
    await _when_live(store, session.id, reader, settle=0.8)
    request = await store.emit_event(session.id, EventType.LLM_REQUEST, {})
    for seq in range(5):
        await _frame(store, session.id, request, seq, content=f"{seq} ")
    await store.emit_event(session.id, EventType.LLM_RESPONSE, {"message": {"content": "0 1 2 3 4 "}})
    await store.emit_event(session.id, EventType.TURN_SUMMARY, {})
    await _end_turn(store, session.id)
    events = await reader
    kinds = [k for k, _ in events if k not in ("stream.live", "llm.delta")]
    assert kinds == ["llm.request", "llm.response", "turn.summary", "session.complete", "session.done"]


async def test_frames_of_a_call_wait_for_the_events_before_it_when_a_notification_is_lost(live, session_factory):
    app, client, store, session, token = live
    silent = SessionStore(session_factory)  # writes events without telling the channel
    reader = asyncio.create_task(_watch(client, session.id, token))
    await _when_live(store, session.id, reader)
    first = await store.emit_event(session.id, EventType.LLM_REQUEST, {})
    await _frame(store, session.id, first, 0, content="one")
    await silent.emit_event(session.id, EventType.LLM_RESPONSE, {"message": {"content": "one"}})  # notification lost
    second = await silent.emit_event(session.id, EventType.LLM_REQUEST, {})
    await _frame(store, session.id, second, 0, content="two")
    await store.emit_event(session.id, EventType.LLM_RESPONSE, {"message": {"content": "two"}})
    await _end_turn(store, session.id)
    events = await reader
    seen = [(k, json.loads(d).get("content") if k == "llm.delta" else None) for k, d in events
            if k in ("llm.request", "llm.delta", "llm.response")]
    assert seen == [("llm.request", None), ("llm.delta", "one"), ("llm.response", None),
                    ("llm.request", None), ("llm.delta", "two"), ("llm.response", None)]


async def test_a_synthetic_response_mid_reply_does_not_cut_the_reply(live):
    """A channel delivery writes a synthetic llm.response of its own: it ends no model call."""
    app, client, store, session, token = live
    reader = asyncio.create_task(_watch(client, session.id, token))
    await _when_live(store, session.id, reader)
    request = await store.emit_event(session.id, EventType.LLM_REQUEST, {})
    await _frame(store, session.id, request, 0, content="before ")
    await store.emit_event(session.id, EventType.LLM_RESPONSE, {"synthetic": "outbound_text", "message": {"content": "hi"}})
    await _frame(store, session.id, request, 1, content="after")
    await store.emit_event(session.id, EventType.LLM_RESPONSE, {"message": {"content": "before after"}})
    await _end_turn(store, session.id)
    events = await reader
    assert "".join(json.loads(d)["content"] for k, d in events if k == "llm.delta") == "before after"


# ---------------------------------------------------------------------------
# OpenAI-compatible streaming
# ---------------------------------------------------------------------------


async def _openai_text(store, redis_client, session_id, after, agent) -> tuple[str, str]:
    reader = _TurnReader(store, session_id, after)
    content, reasoning = [], []
    stream = _stream_turn(reader=reader, redis=redis_client, session_id=session_id, completion_id="c",
                          model="m", created=0, include_usage=False)

    async def collect():
        async for line in stream:
            if not line.startswith("data: {"):
                continue
            delta = json.loads(line[6:])["choices"]
            if delta and delta[0]["delta"].get("content"):
                content.append(delta[0]["delta"]["content"])
            if delta and delta[0]["delta"].get("reasoning_content"):
                reasoning.append(delta[0]["delta"]["reasoning_content"])

    task = asyncio.create_task(collect())
    await asyncio.sleep(0.3)  # subscribed and read
    await agent()
    await asyncio.wait_for(task, 10)
    return "".join(content), "".join(reasoning)


async def test_the_openai_stream_passes_a_replys_frames_on_as_chunks(session_factory, redis_client):
    store = SessionStore(session_factory, redis=redis_client)
    org_id = await create_org(session_factory)
    session = await store.create_session(user_id=await create_user(session_factory, org_id), org_id=org_id, agent_id="a")
    user = await store.emit_event(session.id, EventType.USER_MESSAGE, {"content": "hi"})

    async def agent():
        request = await store.emit_event(session.id, EventType.LLM_REQUEST, {})
        await _frame(store, session.id, request, 0, reasoning="Short ")
        await _frame(store, session.id, request, 1, reasoning="thought.")
        await _frame(store, session.id, request, 2, content="Hello")
        await _frame(store, session.id, request, 3, content=" there.")
        await store.emit_event(session.id, EventType.LLM_THINKING, {"reasoning": "Short thought."})
        await store.emit_event(session.id, EventType.LLM_RESPONSE, {"message": {"content": "Hello there."}})
        await store.emit_event(session.id, EventType.SESSION_COMPLETE, {})

    content, reasoning = await _openai_text(store, redis_client, session.id, user, agent)
    assert (content, reasoning) == ("Hello there.", "Short thought.")


@pytest.mark.parametrize("missed", ["the start", "the end"])
async def test_an_openai_stream_that_missed_frames_still_sends_the_whole_reply(session_factory, redis_client, missed):
    """A retry attaching to a turn under way joins after its first frames; a reader can fall behind its last."""
    store = SessionStore(session_factory, redis=redis_client)
    org_id = await create_org(session_factory)
    session = await store.create_session(user_id=await create_user(session_factory, org_id), org_id=org_id, agent_id="a")
    user = await store.emit_event(session.id, EventType.USER_MESSAGE, {"content": "hi"})

    async def agent():
        request = await store.emit_event(session.id, EventType.LLM_REQUEST, {})
        pieces = ["One, ", "two, ", "three."]
        for seq, piece in enumerate(pieces):
            if (missed == "the start" and seq == 0) or (missed == "the end" and seq == 2):
                continue  # this reader never got it
            await _frame(store, session.id, request, seq, content=piece)
        await store.emit_event(session.id, EventType.LLM_RESPONSE, {"message": {"content": "One, two, three."}})
        await store.emit_event(session.id, EventType.SESSION_COMPLETE, {})

    content, _ = await _openai_text(store, redis_client, session.id, user, agent)
    assert content == "One, two, three."


async def test_an_openai_stream_leaves_out_the_frames_of_the_turn_before(session_factory, redis_client):
    store = SessionStore(session_factory, redis=redis_client)
    org_id = await create_org(session_factory)
    session = await store.create_session(user_id=await create_user(session_factory, org_id), org_id=org_id, agent_id="a")
    old_request = await store.emit_event(session.id, EventType.LLM_REQUEST, {})
    user = await store.emit_event(session.id, EventType.USER_MESSAGE, {"content": "new question"})

    async def agent():
        await _frame(store, session.id, old_request, 7, content="old tail ")
        request = await store.emit_event(session.id, EventType.LLM_REQUEST, {})
        await _frame(store, session.id, request, 0, content="New answer.")
        await store.emit_event(session.id, EventType.LLM_RESPONSE, {"message": {"content": "New answer."}})
        await store.emit_event(session.id, EventType.SESSION_COMPLETE, {})

    content, _ = await _openai_text(store, redis_client, session.id, user, agent)
    assert content == "New answer."


async def test_an_openai_stream_whose_frames_stop_coming_still_sends_the_final_answer(session_factory, redis_client):
    """Frames of the first call heard, none of the last (a channel that failed): the answer comes from the log."""
    store = SessionStore(session_factory, redis=redis_client)
    org_id = await create_org(session_factory)
    session = await store.create_session(user_id=await create_user(session_factory, org_id), org_id=org_id, agent_id="a")
    user = await store.emit_event(session.id, EventType.USER_MESSAGE, {"content": "hi"})

    async def agent():
        request = await store.emit_event(session.id, EventType.LLM_REQUEST, {})
        await _frame(store, session.id, request, 0, content="Let me check. ")
        await store.emit_event(session.id, EventType.LLM_RESPONSE, {"message": {
            "content": "Let me check. ", "tool_calls": [{"id": "1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]}})
        await store.emit_event(session.id, EventType.LLM_REQUEST, {})  # no frame of this call is heard
        await store.emit_event(session.id, EventType.LLM_RESPONSE, {"message": {"content": "It is 4,97."}})
        await store.emit_event(session.id, EventType.SESSION_COMPLETE, {})

    content, _ = await _openai_text(store, redis_client, session.id, user, agent)
    assert content == "Let me check. It is 4,97."


async def test_an_openai_stream_with_a_frame_missing_sends_the_rest_at_the_response(session_factory, redis_client):
    store = SessionStore(session_factory, redis=redis_client)
    org_id = await create_org(session_factory)
    session = await store.create_session(user_id=await create_user(session_factory, org_id), org_id=org_id, agent_id="a")
    user = await store.emit_event(session.id, EventType.USER_MESSAGE, {"content": "hi"})

    async def agent():
        request = await store.emit_event(session.id, EventType.LLM_REQUEST, {})
        await _frame(store, session.id, request, 0, content="A, ")
        await _frame(store, session.id, request, 2, content="C.")  # frame 1 never arrived
        await store.emit_event(session.id, EventType.LLM_RESPONSE, {"message": {"content": "A, B, C."}})
        await store.emit_event(session.id, EventType.SESSION_COMPLETE, {})

    content, _ = await _openai_text(store, redis_client, session.id, user, agent)
    assert content == "A, B, C."


@pytest.mark.parametrize("stored", ["Euro e 4,97 lei.", "\n\nEuro e 4,97 lei."])
async def test_an_openai_stream_completes_a_reply_stored_stripped_or_as_streamed(session_factory, redis_client, stored):
    """A call with reasoning is stored stripped, one without as streamed; its frames kept the leading whitespace."""
    store = SessionStore(session_factory, redis=redis_client)
    org_id = await create_org(session_factory)
    session = await store.create_session(user_id=await create_user(session_factory, org_id), org_id=org_id, agent_id="a")
    user = await store.emit_event(session.id, EventType.USER_MESSAGE, {"content": "hi"})

    async def agent():
        request = await store.emit_event(session.id, EventType.LLM_REQUEST, {})
        await _frame(store, session.id, request, 0, content="\n\nEuro e ")
        await _frame(store, session.id, request, 2, content="lei.")  # frame 1 lost
        await store.emit_event(session.id, EventType.LLM_RESPONSE, {"message": {"content": stored}})
        await store.emit_event(session.id, EventType.SESSION_COMPLETE, {})

    content, _ = await _openai_text(store, redis_client, session.id, user, agent)
    assert content.strip() == "Euro e 4,97 lei."


async def test_an_openai_turn_with_frames_and_delta_rows_sends_each_text_once(session_factory, redis_client):
    """A turn run partly by a worker sending frames and partly by one writing rows (a transport switch)."""
    store = SessionStore(session_factory, redis=redis_client)
    org_id = await create_org(session_factory)
    session = await store.create_session(user_id=await create_user(session_factory, org_id), org_id=org_id, agent_id="a")
    user = await store.emit_event(session.id, EventType.USER_MESSAGE, {"content": "hi"})

    async def agent():
        request = await store.emit_event(session.id, EventType.LLM_REQUEST, {})
        await _frame(store, session.id, request, 0, content="Checking. ")
        await store.emit_event(session.id, EventType.LLM_RESPONSE, {"message": {
            "content": "Checking. ", "tool_calls": [{"id": "1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]}})
        await store.emit_event(session.id, EventType.LLM_REQUEST, {})
        await store.emit_event(session.id, EventType.LLM_DELTA, {"content": "Done."})
        await store.emit_event(session.id, EventType.LLM_RESPONSE, {"message": {"content": "Done."}})
        await store.emit_event(session.id, EventType.SESSION_COMPLETE, {})

    content, _ = await _openai_text(store, redis_client, session.id, user, agent)
    assert content == "Checking. Done."


# ---------------------------------------------------------------------------
# Voice
# ---------------------------------------------------------------------------


async def test_a_call_speaks_a_replys_frames_and_stops_at_its_answer(session_factory, redis_client):
    store = SessionStore(session_factory, redis=redis_client)
    org = await create_org(session_factory)
    user = await create_user(session_factory, org)
    s = await store.create_session(user_id=user, org_id=org, agent_id="agent-1", channel="voice")
    call = CallSession(store=store, redis=redis_client, session_id=s.id, org_id=org, agent_id="agent-1",
                       user_id=user, caller="40722000111")
    after = await call.send("Cât e euro?")

    async def agent():
        await asyncio.sleep(0.05)
        old = after  # a frame of the turn the caller cut off, still in flight
        await _frame(store, s.id, old - 1, 9, content="nobody asked ")
        request = await store.emit_event(s.id, EventType.LLM_REQUEST, {})
        await _frame(store, s.id, request, 0, reasoning="never spoken")
        await _frame(store, s.id, request, 1, content="Euro e ")
        await _frame(store, s.id, request, 2, content="4,97 lei.")
        await store.emit_event(s.id, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": "Euro e 4,97 lei."}})
        await _frame(store, s.id, request, 3, content=" stale")

    writer = asyncio.create_task(agent())
    text_heard = "".join([t async for t in call.stream(after)])
    await writer
    assert text_heard == "Euro e 4,97 lei."


@pytest.mark.parametrize("stored", ["Euro e 4,97 lei.", "\n\nEuro e 4,97 lei."])
async def test_a_call_speaks_the_rest_of_an_answer_whose_frames_were_lost(session_factory, redis_client, stored):
    store = SessionStore(session_factory, redis=redis_client)
    org = await create_org(session_factory)
    user = await create_user(session_factory, org)
    s = await store.create_session(user_id=user, org_id=org, agent_id="agent-1", channel="voice")
    call = CallSession(store=store, redis=redis_client, session_id=s.id, org_id=org, agent_id="agent-1",
                       user_id=user, caller="40722000111")
    after = await call.send("Cât e euro?")

    async def agent():
        await asyncio.sleep(0.05)
        request = await store.emit_event(s.id, EventType.LLM_REQUEST, {})
        await _frame(store, s.id, request, 0, content="\n\nEuro e ")
        await _frame(store, s.id, request, 2, content="lei.")  # frame 1 lost
        await store.emit_event(s.id, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": stored}})

    writer = asyncio.create_task(agent())
    heard = "".join([t async for t in call.stream(after)])
    await writer
    assert heard.strip() == "Euro e 4,97 lei."


async def test_a_call_with_frames_and_delta_rows_speaks_each_text_once(session_factory, redis_client):
    """A turn run partly by a worker sending frames and partly by one writing rows (a transport switch)."""
    store = SessionStore(session_factory, redis=redis_client)
    org = await create_org(session_factory)
    user = await create_user(session_factory, org)
    s = await store.create_session(user_id=user, org_id=org, agent_id="agent-1", channel="voice")
    call = CallSession(store=store, redis=redis_client, session_id=s.id, org_id=org, agent_id="agent-1",
                       user_id=user, caller="40722000111")
    after = await call.send("Cât e euro?")

    async def agent():
        await asyncio.sleep(0.05)
        request = await store.emit_event(s.id, EventType.LLM_REQUEST, {})
        await _frame(store, s.id, request, 0, content="Checking. ")
        await store.emit_event(s.id, EventType.LLM_RESPONSE, {"message": {
            "role": "assistant", "content": "Checking. ",
            "tool_calls": [{"id": "1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]}})
        await store.emit_event(s.id, EventType.LLM_REQUEST, {})
        await store.emit_event(s.id, EventType.LLM_DELTA, {"content": "Done."})
        await store.emit_event(s.id, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": "Done."}})

    writer = asyncio.create_task(agent())
    heard = "".join([t async for t in call.stream(after)])
    await writer
    assert heard == "Checking. Done."


# ---------------------------------------------------------------------------
# The opt-in cleanup of delta rows of finished replies
# ---------------------------------------------------------------------------


async def test_the_cleanup_removes_old_delta_rows_and_keeps_replays_reasoning_count(session_factory, engine):
    store = SessionStore(session_factory)
    org_id = await create_org(session_factory)
    session = await store.create_session(user_id=await create_user(session_factory, org_id), org_id=org_id, agent_id="a")
    sid = session.id
    request = await store.emit_event(sid, EventType.LLM_REQUEST, {})
    for piece in ("a", "b", "c"):
        await store.emit_event(sid, EventType.LLM_DELTA, {"reasoning": piece})
    await store.emit_event(sid, EventType.LLM_DELTA, {"content": "x"})
    thinking = await store.emit_event(sid, EventType.LLM_THINKING, {"reasoning": "abc"})
    response = await store.emit_event(sid, EventType.LLM_RESPONSE, {"message": {"content": "x"}})
    fresh = await store.emit_event(sid, EventType.LLM_DELTA, {"content": "still streaming"})
    # An old reply that never ended (its worker died mid-stream, no event after it): not touched either.
    other = await store.create_session(user_id=await create_user(session_factory, org_id), org_id=org_id, agent_id="a")
    await store.emit_event(other.id, EventType.LLM_REQUEST, {})
    stuck = await store.emit_event(other.id, EventType.LLM_DELTA, {"content": "cut off"})
    async with session_factory() as db:
        await db.execute(update(EventRow).where(EventRow.session_id.in_([sid, other.id]), EventRow.id != fresh)
                         .values(created_at=text("now() - interval '2 days'")))
        await db.commit()
    with pytest.raises(ValueError):
        await purge_llm_delta_rows(engine, older_than=timedelta(minutes=5), execute=True)
    before = await store.get_events(sid, exclude_types=[EventType.LLM_DELTA])
    assert next(e for e in before if e.id == thinking).data["reasoning_delta_count"] == 3

    dry = await purge_llm_delta_rows(engine, older_than=timedelta(days=1))
    assert dry.dry_run and dry.deleted == 0 and dry.candidates >= 4
    report = await purge_llm_delta_rows(engine, older_than=timedelta(days=1), batch=2, execute=True)
    assert report.deleted >= 4 and report.thinking_counts_written >= 1

    rows = {e.id: e for e in await store.get_events(sid)}
    assert fresh in rows  # newer than the cutoff: a reply possibly still streaming
    assert [e.type for e in rows.values() if e.type == "llm.delta"] == ["llm.delta"]
    assert {request, thinking, response} <= set(rows)
    # Replay shows the same count with the rows gone: it was written into the llm.thinking.
    assert rows[thinking].data["reasoning_delta_count"] == 3
    assert stuck in {e.id for e in await store.get_events(other.id)}
