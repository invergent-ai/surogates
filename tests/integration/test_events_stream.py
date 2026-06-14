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


async def _read_sse_events(response, *, until_types: set[str], deadline_s: float):
    """Consume an SSE response and return a list of ``(event, data)`` pairs.

    Returns once **any** event in ``until_types`` has been observed (so the
    test can close the stream and assert), or once ``deadline_s`` has
    elapsed (so a hung handler still surfaces as a failure rather than
    blocking forever).
    """
    received: list[tuple[str, str]] = []
    event_type = ""
    data_lines: list[str] = []

    async def _consume():
        nonlocal event_type, data_lines
        async for line in response.aiter_lines():
            if line == "":
                if event_type:
                    received.append((event_type, "\n".join(data_lines)))
                    if event_type in until_types:
                        return
                event_type = ""
                data_lines = []
                continue
            if line.startswith(":"):
                # SSE comment (e.g. ``: connected``) — ignore.
                continue
            if line.startswith("event:"):
                event_type = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].lstrip())
            # id:/retry: are not needed for the assertions here.

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
