"""Integration tests for the SSE event-streaming endpoint.

Covers the terminal-status race that previously caused the
"second message after assistant responds does nothing" bug:

1. ``test_terminal_close_emits_session_done`` — a session that is
   genuinely ``completed`` with no pending writes closes within the
   grace window with a ``session.done`` event.

2. ``test_race_recovery_streams_resumed_events`` — a session that is
   ``completed`` when the SSE opens, then transitions back to ``active``
   via a SESSION_RESUME publish during the grace window, must deliver
   the new events instead of fast-closing.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from surogates.db.models import InboxItem
from surogates.session.events import EventType
from surogates.session.store import SessionStore
from surogates.tenant.auth.jwt import create_access_token
from surogates.tenant.credentials import CredentialVault

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def app(session_factory, redis_client, pg_url, redis_url):
    os.environ["SUROGATES_DB_URL"] = pg_url
    os.environ["SUROGATES_REDIS_URL"] = redis_url

    from surogates.api.app import create_app
    from surogates.config import Settings
    from surogates.storage.backend import create_backend

    application = create_app()
    application.state.session_factory = session_factory
    application.state.redis = redis_client
    application.state.session_store = SessionStore(
        session_factory,
        redis=redis_client,
    )
    application.state.settings = Settings()
    application.state.storage = create_backend(application.state.settings)
    application.state.credential_vault = CredentialVault(
        session_factory,
        Fernet.generate_key(),
    )
    return application


@pytest_asyncio.fixture(loop_scope="session")
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        timeout=10.0,
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_completed_session(session_factory, session_store):
    """Set up a session row + user JWT, then mark the session ``completed``.

    A completed session is the entry condition for both the legacy
    fast-close path and the race-recovery grace window.
    """
    org_id = await create_org(session_factory)
    user_id = uuid.uuid4()
    await create_user(session_factory, org_id, user_id=user_id)
    token = create_access_token(
        org_id,
        user_id,
        {"sessions:read", "sessions:write"},
    )
    session = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id="test-agent",
    )
    await session_store.update_session_status(session.id, "completed")
    return session, token


async def _read_sse_events(
    response,
    *,
    until_types: set[str],
    deadline_s: float,
    ids: list[str | None] | None = None,
    comments: list[str] | None = None,
):
    """Consume an SSE response and return a list of ``(event, data)`` pairs.

    Returns once **any** event in ``until_types`` has been observed (so the
    test can close the stream and assert), or once ``deadline_s`` has
    elapsed (so a hung handler still surfaces as a failure rather than
    blocking forever). ``ids``, when given, gets each event's id, and
    ``comments`` each comment line's text.
    """
    received: list[tuple[str, str]] = []
    event_type = ""
    event_id: str | None = None
    data_lines: list[str] = []

    async def _consume():
        nonlocal event_type, event_id, data_lines
        async for line in response.aiter_lines():
            if line == "":
                if event_type:
                    received.append((event_type, "\n".join(data_lines)))
                    if ids is not None:
                        ids.append(event_id)
                    if event_type in until_types:
                        return
                event_type = ""
                event_id = None
                data_lines = []
                continue
            if line.startswith(":"):
                # SSE comment (e.g. ``: connected``): no event.
                if comments is not None:
                    comments.append(line[1:].strip())
                continue
            if line.startswith("event:"):
                event_type = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].lstrip())
            elif line.startswith("id:"):
                event_id = line.split(":", 1)[1].strip()

    try:
        await asyncio.wait_for(_consume(), timeout=deadline_s)
    except asyncio.TimeoutError:
        pass
    return received


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_terminal_close_emits_session_done(
    session_factory,
    session_store,
    client,
):
    """A truly terminal session — no pending writes — closes with session.done.

    The grace window inside ``event_generator`` waits up to ``_POLL_INTERVAL``
    (500 ms) before declaring the session done, so we allow ~2 s for the
    close to surface. Without the patched grace window this would have
    closed via the pre-loop fast-path almost instantly; with the patched
    version it closes via the grace-window-then-recheck path. Either way
    the externally-observable behaviour is identical: one ``session.done``
    event and the stream ends.
    """
    session, token = await _create_completed_session(session_factory, session_store)

    async with client.stream(
        "GET",
        f"/v1/sessions/{session.id}/events?after=0",
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        assert response.status_code == 200
        events = await _read_sse_events(
            response,
            until_types={"session.done"},
            deadline_s=2.0,
        )

    types = [event_type for event_type, _ in events]
    assert "session.done" in types, (
        f"expected session.done on a terminal session, got: {types}"
    )


async def test_race_recovery_streams_resumed_events(
    session_factory,
    app,
    client,
    monkeypatch,
):
    """Resume-during-grace must deliver the new events, not fast-close.

    Reproduces the production race: the SSE opens while the session is
    ``completed``. After the SSE has subscribed to the session pubsub
    channel but before it emits ``session.done``, a SESSION_RESUME event
    is committed (mimicking POST /messages flipping the session back to
    active and emitting a resume event). The publish must wake the grace
    window, the handler must re-check status, find ``active``, and stream
    the new events through.

    Uses the app's redis-aware ``session_store`` (not the conftest fixture)
    so ``emit_event`` actually publishes on ``surogates:session:{id}`` —
    the publish is the entire point of the race guard.

    httpx's ``ASGITransport`` buffers the response body and only returns the
    Response object after the ASGI app finishes — it does not stream live.
    For a terminal-closing handler that's invisible (the handler returns
    after ``session.done``), but here the handler keeps the stream open
    after recovery. Shorten ``_MAX_STREAM_DURATION`` so it exits via the
    ``stream.timeout`` branch a moment after the recovered events are
    flushed; the race-guard behaviour we care about (which events arrive,
    and that ``session.done`` does not) is independent of when the stream
    eventually closes.
    """
    import surogates.api.routes.events as events_module
    monkeypatch.setattr(events_module, "_MAX_STREAM_DURATION", 2)

    redis_store: SessionStore = app.state.session_store
    session, token = await _create_completed_session(session_factory, redis_store)

    async def _flip_to_active_after_subscribe():
        # Give the SSE handler time to enter event_generator and subscribe
        # to ``surogates:session:{id}``. The subscribe must precede the
        # publish — that's the whole point of the race guard.
        await asyncio.sleep(0.25)
        await redis_store.update_session_status(session.id, "active")
        await redis_store.emit_event(
            session.id,
            EventType.SESSION_RESUME,
            {},
        )
        await redis_store.emit_event(
            session.id,
            EventType.USER_MESSAGE,
            {"content": "how are you ?"},
        )

    flipper = asyncio.create_task(_flip_to_active_after_subscribe())
    try:
        async with client.stream(
            "GET",
            f"/v1/sessions/{session.id}/events?after=0",
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            assert response.status_code == 200
            events = await _read_sse_events(
                response,
                until_types={"user.message"},
                deadline_s=5.0,
            )
    finally:
        await flipper

    types = [event_type for event_type, _ in events]
    assert "session.resume" in types, (
        f"expected session.resume to reach the client; got: {types}"
    )
    assert "user.message" in types, (
        f"expected user.message to reach the client; got: {types}"
    )
    assert "session.done" not in types, (
        f"session.done must not be emitted when the race is recovered; "
        f"got: {types}"
    )


async def test_keepalive_comment_on_idle_active_session(
    session_factory,
    session_store,
    client,
    monkeypatch,
):
    """An active session with no new events still receives periodic SSE
    keepalive comments, so a proxy's idle-connection timeout (the
    coordinator's minutes-long waits between wakes) can't silently drop the
    live stream."""
    import surogates.api.routes.events as events_module
    monkeypatch.setattr(events_module, "_MAX_STREAM_DURATION", 1)
    monkeypatch.setattr(events_module, "_KEEPALIVE_INTERVAL", 0.2)

    org_id = await create_org(session_factory)
    user_id = uuid.uuid4()
    await create_user(session_factory, org_id, user_id=user_id)
    token = create_access_token(
        org_id, user_id, {"sessions:read", "sessions:write"},
    )
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )  # left active, no events

    lines: list[str] = []

    async def _collect(response):
        async for line in response.aiter_lines():
            lines.append(line)

    async with client.stream(
        "GET",
        f"/v1/sessions/{session.id}/events?after=0",
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        assert response.status_code == 200
        try:
            await asyncio.wait_for(_collect(response), timeout=5.0)
        except asyncio.TimeoutError:
            pass

    assert any("keepalive" in line for line in lines), (
        f"expected a keepalive comment on an idle active session, got: {lines[:30]}"
    )


async def _completed_chat(session_factory, store: SessionStore):
    """A chat between its turns, as the harness leaves one: an earlier turn, and ``completed``.

    Returns the session, a user token, and the id of the earlier turn's ``session.complete``.
    """
    org_id = await create_org(session_factory)
    user_id = uuid.uuid4()
    await create_user(session_factory, org_id, user_id=user_id)
    token = create_access_token(org_id, user_id, {"sessions:read", "sessions:write"})
    session = await store.create_session(user_id=user_id, org_id=org_id, agent_id="test-agent")
    await store.emit_event(session.id, EventType.USER_MESSAGE, {"content": "an earlier turn"})
    last = await store.emit_event(session.id, EventType.SESSION_COMPLETE, {})
    await store.update_session_status(session.id, "completed")
    return session, token, last


async def test_watch_follows_a_chat_from_its_newest_event_across_its_turns(
    session_factory,
    app,
    client,
    monkeypatch,
):
    """``after=-1&watch=1`` follows a chat that sits ``completed`` between turns.

    Surogate Desktop follows the chat its window shows this way, for the end
    of the next turn: the agent leaves that turn out of the inbox while a page
    streams the chat. The stream names its starting cursor first, keeps its
    keepalives through the idle wait, and gives the next turn, which flips the
    chat to ``active`` and back to ``completed``, with none of the earlier one.
    As in the race test above, the stream is cut short so the buffered
    response comes back.
    """
    import surogates.api.routes.events as events_module
    monkeypatch.setattr(events_module, "_MAX_STREAM_DURATION", 3)
    monkeypatch.setattr(events_module, "_KEEPALIVE_INTERVAL", 0.2)

    redis_store: SessionStore = app.state.session_store
    session, token, last = await _completed_chat(session_factory, redis_store)

    async def _next_turn():
        # Past the grace window in which a stream without watch closes on ``completed``.
        await asyncio.sleep(1.0)
        await redis_store.update_session_status(session.id, "active")
        await redis_store.emit_event(session.id, EventType.USER_MESSAGE, {"content": "the next turn"})
        await redis_store.emit_event(session.id, EventType.SESSION_COMPLETE, {})
        await redis_store.update_session_status(session.id, "completed")

    ids: list[str | None] = []
    comments: list[str] = []
    turn = asyncio.create_task(_next_turn())
    try:
        async with client.stream(
            "GET",
            f"/v1/sessions/{session.id}/events?after=-1&watch=1",
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            assert response.status_code == 200
            events = await _read_sse_events(
                response,
                until_types={"stream.timeout"},
                deadline_s=6.0,
                ids=ids,
                comments=comments,
            )
    finally:
        await turn

    # Then it ends at its longest, as every stream does, for the watcher to take up after the last id.
    assert events == [
        ("stream.start", "{}"),
        ("user.message", '{"content": "the next turn"}'),
        ("session.complete", "{}"),
        ("stream.timeout", '{"reason": "max_duration_exceeded"}'),
    ]
    assert ids[0] == str(last)
    assert "keepalive" in comments


async def test_watch_ends_on_an_archived_chat(session_factory, app, client):
    """A watching stream ends as any other once the chat is archived."""
    redis_store: SessionStore = app.state.session_store
    session, token, _last = await _completed_chat(session_factory, redis_store)
    await redis_store.update_session_status(session.id, "archived")

    async with client.stream(
        "GET",
        f"/v1/sessions/{session.id}/events?after=-1&watch=1",
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        events = await _read_sse_events(response, until_types={"session.done"}, deadline_s=3.0)

    assert events[-1] == ("session.done", '{"reason": "archived", "status": "archived"}')


async def test_without_watch_a_completed_chat_still_ends_its_stream(session_factory, app, client):
    """Every other caller is as before: ``after=-1`` alone, on a chat between
    turns, ends with ``session.done`` within the grace window."""
    redis_store: SessionStore = app.state.session_store
    session, token, _last = await _completed_chat(session_factory, redis_store)

    async with client.stream(
        "GET",
        f"/v1/sessions/{session.id}/events?after=-1",
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        events = await _read_sse_events(response, until_types={"session.done"}, deadline_s=3.0)

    assert events == [("session.done", '{"reason": "completed", "status": "completed"}')]


def _counting_reads(monkeypatch, store: SessionStore, session_id) -> list[tuple[float, str, list[int]]]:
    """Each database read of *session_id*'s events or status: when, which, and the event ids it gave."""
    loop = asyncio.get_running_loop()
    reads: list[tuple[float, str, list[int]]] = []
    get_events, get_session = store.get_events, store.get_session

    async def counted_events(asked, *args, **kwargs):
        events = await get_events(asked, *args, **kwargs)
        if asked == session_id:
            reads.append((loop.time(), "events", [event.id for event in events]))
        return events

    async def counted_session(asked, *args, **kwargs):
        if asked == session_id:
            reads.append((loop.time(), "session", []))
        return await get_session(asked, *args, **kwargs)

    monkeypatch.setattr(store, "get_events", counted_events)
    monkeypatch.setattr(store, "get_session", counted_session)
    return reads


async def test_a_watch_reads_the_chat_only_when_woken_or_at_its_keepalive(
    session_factory,
    app,
    client,
    monkeypatch,
):
    """A watched chat between its turns costs no database read while nothing happens.

    Surogate Desktop holds its watch for as long as its window is away, which
    can be days. Every ``emit_event`` publishes on the session's channel after
    its commit, so the watch waits there: it reads the chat when a publish
    wakes it, and at each keepalive. A stream that polled would read it every
    ``_POLL_INTERVAL``.
    """
    import surogates.api.routes.events as events_module
    monkeypatch.setattr(events_module, "_MAX_STREAM_DURATION", 3)
    monkeypatch.setattr(events_module, "_KEEPALIVE_INTERVAL", 1.0)
    monkeypatch.setattr(events_module, "_POLL_INTERVAL", 0.05)

    redis_store: SessionStore = app.state.session_store
    session, token, _last = await _completed_chat(session_factory, redis_store)
    reads = _counting_reads(monkeypatch, redis_store, session.id)
    loop = asyncio.get_running_loop()
    turn_at: list[float] = []
    turn_id: list[int] = []

    async def _next_turn():
        await asyncio.sleep(1.5)
        turn_at.append(loop.time())
        turn_id.append(await redis_store.emit_event(session.id, EventType.USER_MESSAGE, {"content": "the next turn"}))

    turn = asyncio.create_task(_next_turn())
    try:
        async with client.stream(
            "GET",
            f"/v1/sessions/{session.id}/events?after=-1&watch=1",
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            events = await _read_sse_events(response, until_types={"stream.timeout"}, deadline_s=6.0)
    finally:
        await turn

    assert ("user.message", '{"content": "the next turn"}') in events
    # Before the turn: the access check, then the chat's events and status at the start, and again at the keepalive.
    assert [kind for at, kind, _ids in reads if at < turn_at[0]] == ["session", "events", "session", "events", "session"]
    # The turn's publish woke the watch: it read the turn at once, not at the next keepalive.
    woken = next(at for at, _kind, ids in reads if turn_id[0] in ids)
    assert woken - turn_at[0] < 0.3


class _SubscriptionInFlight:
    """A pubsub whose subscription Redis takes a moment after the stream sent it, once *meanwhile* ran.

    As redis-py's own: ``subscribe`` and ``psubscribe`` send the command and return, and no reply
    can be read before Redis takes it. What *meanwhile* publishes reaches nobody.
    """

    def __init__(self, real, meanwhile):
        self._real = real
        self._meanwhile = meanwhile
        self._taken = asyncio.Event()

    def __getattr__(self, name):
        sent = getattr(self._real, name)
        if name not in ("subscribe", "psubscribe"):
            return sent

        async def in_flight(*channels):
            async def taken():
                await asyncio.sleep(0.2)
                await self._meanwhile()
                await sent(*channels)
                self._taken.set()

            self._taking = asyncio.create_task(taken())

        return in_flight

    async def get_message(self, *, ignore_subscribe_messages: bool = False, timeout: float = 0.0):
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            await asyncio.wait_for(self._taken.wait(), timeout)
        except asyncio.TimeoutError:
            return None
        return await self._real.get_message(
            ignore_subscribe_messages=ignore_subscribe_messages,
            timeout=max(0.0, timeout - (loop.time() - started)),
        )


async def test_a_watch_reads_the_chat_once_redis_has_taken_its_subscription(
    session_factory,
    app,
    client,
    monkeypatch,
):
    """A turn published while the watch's subscription is on its way to Redis is still read at once.

    redis-py sends SUBSCRIBE without waiting for Redis to take it. A watch
    that read the chat before then, and waited on the channel after, would
    hear of a turn published in between only at its next keepalive.
    """
    import surogates.api.routes.events as events_module
    monkeypatch.setattr(events_module, "_MAX_STREAM_DURATION", 3)
    monkeypatch.setattr(events_module, "_KEEPALIVE_INTERVAL", 2.0)

    redis_store: SessionStore = app.state.session_store
    session, token, _last = await _completed_chat(session_factory, redis_store)
    reads = _counting_reads(monkeypatch, redis_store, session.id)
    loop = asyncio.get_running_loop()
    turn_at: list[float] = []
    turn_id: list[int] = []

    async def _next_turn():
        turn_at.append(loop.time())
        turn_id.append(await redis_store.emit_event(session.id, EventType.USER_MESSAGE, {"content": "the next turn"}))

    pubsub = app.state.redis.pubsub
    monkeypatch.setattr(app.state.redis, "pubsub", lambda: _SubscriptionInFlight(pubsub(), _next_turn))

    async with client.stream(
        "GET",
        f"/v1/sessions/{session.id}/events?after=-1&watch=1",
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        events = await _read_sse_events(response, until_types={"stream.timeout"}, deadline_s=6.0)

    assert ("user.message", '{"content": "the next turn"}') in events
    woken = next(at for at, _kind, ids in reads if turn_id[0] in ids)
    assert woken - turn_at[0] < 0.3


async def test_a_watch_leaves_the_chat_its_inbox_items(session_factory, app, client, monkeypatch):
    """A watch is no live viewer: the chat's check-ins and completions still reach the inbox.

    The agent leaves a chat's acknowledge-only items out of the inbox while a
    page streams it, a page being an exact subscriber of the chat's channel.
    Surogate Desktop tells only the end of a turn from its watch, so a
    check-in raised while only the desktop watches belongs in the inbox.
    """
    import surogates.api.routes.events as events_module
    monkeypatch.setattr(events_module, "_MAX_STREAM_DURATION", 2)

    redis_store: SessionStore = app.state.session_store
    session, token, _last = await _completed_chat(session_factory, redis_store)
    checked_in: list[int] = []

    async def _check_in():
        await asyncio.sleep(0.5)
        checked_in.append(await redis_store.emit_event(
            session.id,
            EventType.INBOX_PROGRESS_CHECKIN,
            {"iterations": 3, "elapsed_seconds": 120, "progress_summary": "Halfway."},
        ))

    check_in = asyncio.create_task(_check_in())
    try:
        async with client.stream(
            "GET",
            f"/v1/sessions/{session.id}/events?after=-1&watch=1",
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            events = await _read_sse_events(response, until_types={"stream.timeout"}, deadline_s=5.0)
    finally:
        await check_in

    # The watch was open when the check-in came.
    assert "inbox.progress_checkin" in [event_type for event_type, _data in events]
    async with session_factory() as db:
        rows = (
            await db.execute(select(InboxItem).where(InboxItem.source_event_id == checked_in[0]))
        ).scalars().all()
    assert [row.kind for row in rows] == ["progress_checkin"]
