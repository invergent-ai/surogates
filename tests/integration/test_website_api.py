"""Integration tests for the public-website channel.

Covers the bootstrap path (publishable key + Origin → signed session
cookie + CSRF token), the cookie-authenticated message endpoint with
CSRF double-submit, configured origin enforcement, SSE streaming, and
the deployment-level feature flag that turns the channel off entirely.

The channel is configured via :class:`WebsiteSettings` rather than a
database row — every test sets the relevant fields on
``app.state.settings.website`` before exercising the route.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from uuid import UUID

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from surogates.channels.website_keys import generate_publishable_key
from surogates.channels.website_session import (
    COOKIE_NAME,
    CSRF_HEADER_NAME,
)
from surogates.session.events import EventType
from surogates.session.store import SessionStore
from surogates.tenant.credentials import CredentialVault

from .conftest import create_org

pytestmark = pytest.mark.asyncio(loop_scope="session")


# Default origin used in most tests.
_DEFAULT_ORIGIN = "https://customer.com"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def app(session_factory, redis_client, pg_url, redis_url):
    """FastAPI app wired to the test containers.

    ``enqueue_session`` rejects an empty ``agent_id``, so we override
    ``settings.agent_id`` on the test app.  Setting the env var
    directly would bleed into other tests that expect the empty default
    (some existing prompts_api tests rely on ``session.agent_id == ''``
    matching the server's agent scope).

    Each test creates its own org via ``create_org`` and pokes the
    resulting UUID into ``settings.org_id`` via ``configure_website``;
    the bootstrap route reads org_id from settings.
    """
    os.environ["SUROGATES_DB_URL"] = pg_url
    os.environ["SUROGATES_REDIS_URL"] = redis_url

    from surogates.api.app import create_app
    from surogates.config import Settings
    from surogates.storage.backend import create_backend

    application = create_app()
    application.state.session_factory = session_factory
    application.state.redis = redis_client
    application.state.session_store = SessionStore(
        session_factory, redis=redis_client,
    )
    settings = Settings()
    # In-place attribute override works because pydantic-settings models
    # are mutable unless explicitly frozen; matches how other places
    # inject test-scoped config without polluting process env.
    settings.agent_id = "website-test-agent"
    # ``agent_session_bucket`` requires a non-empty bucket name; the
    # default Settings value is "" because production deployments
    # configure storage at chart install time.  Tests run against the
    # local backend, so any non-empty bucket name suffices.
    settings.storage.bucket = "test-website-bucket"
    application.state.settings = settings
    application.state.storage = create_backend(settings)
    application.state.credential_vault = CredentialVault(
        session_factory, Fernet.generate_key(),
    )
    return application


@pytest_asyncio.fixture(loop_scope="session")
async def client(app):
    # ``https://test`` as base URL lets httpx accept Secure cookies;
    # the session cookie is set with Secure=True because SameSite=None
    # requires it per modern browser policy.
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test",
    ) as c:
        yield c


def configure_website(
    app,
    *,
    org_id: UUID,
    enabled: bool = True,
    publishable_key: str | None = None,
    allowed_origins: str = _DEFAULT_ORIGIN,
) -> str:
    """Set the website-channel settings on *app* and return the publishable key.

    Tests typically need a fresh key per case so a leak between tests
    can't authenticate a follow-up; passing ``publishable_key=None``
    mints one.  ``org_id`` is required because the bootstrap route
    refuses to create sessions when the deployment org isn't set —
    every test explicitly attaches the org it created.
    """
    key = publishable_key if publishable_key is not None else generate_publishable_key()
    app.state.settings.website.enabled = enabled
    app.state.settings.website.publishable_key = key
    app.state.settings.website.allowed_origins = allowed_origins
    app.state.settings.org_id = str(org_id)
    return key


# ---------------------------------------------------------------------------
# Publishable key + origin enforcement at bootstrap
# ---------------------------------------------------------------------------


async def test_bootstrap_happy_path_creates_session_and_cookie(
    app, client: AsyncClient, session_factory, session_store,
):
    """Valid publishable key + allowed origin → 201 with session cookie + CSRF."""
    org_id = await create_org(session_factory)
    key = configure_website(app, org_id=org_id)

    resp = await client.post(
        "/v1/website/sessions",
        headers={
            "Authorization": f"Bearer {key}",
            "Origin": _DEFAULT_ORIGIN,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["agent_name"] == "website-test-agent"
    assert body["session_id"]
    assert body["csrf_token"]

    # Session cookie is HttpOnly + Secure, Path=/ so it works whether
    # the API is mounted at the canonical path or behind the /api/
    # prefix middleware (browsers scope cookies by the URL they see).
    assert COOKIE_NAME in resp.cookies
    set_cookie_raw = resp.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie_raw
    assert "Secure" in set_cookie_raw
    assert "Path=/" in set_cookie_raw

    session = await session_store.get_session(UUID(body["session_id"]))
    assert session.channel == "website"
    assert session.user_id is None
    assert session.org_id == org_id
    assert session.config["website_origin"] == _DEFAULT_ORIGIN


async def test_bootstrap_404_when_channel_disabled(
    app, client: AsyncClient, session_factory,
):
    """A deployment without website.enabled answers 404 like the route doesn't exist."""
    org_id = await create_org(session_factory)
    key = configure_website(app, org_id=org_id, enabled=False)
    resp = await client.post(
        "/v1/website/sessions",
        headers={"Authorization": f"Bearer {key}", "Origin": _DEFAULT_ORIGIN},
    )
    assert resp.status_code == 404


async def test_bootstrap_503_when_publishable_key_empty(
    app, client: AsyncClient, session_factory,
):
    """website.enabled=true with no key is a misconfig — surface 503."""
    org_id = await create_org(session_factory)
    configure_website(app, org_id=org_id, publishable_key="")
    resp = await client.post(
        "/v1/website/sessions",
        headers={"Authorization": "Bearer surg_wk_anything", "Origin": _DEFAULT_ORIGIN},
    )
    assert resp.status_code == 503


async def test_bootstrap_503_when_org_id_missing(
    app, client: AsyncClient, session_factory,
):
    """Visitor sessions need a real org to attach to."""
    org_id = await create_org(session_factory)
    key = configure_website(app, org_id=org_id)
    # Wipe org_id after the helper sets it.
    app.state.settings.org_id = ""
    resp = await client.post(
        "/v1/website/sessions",
        headers={"Authorization": f"Bearer {key}", "Origin": _DEFAULT_ORIGIN},
    )
    assert resp.status_code == 503


async def test_bootstrap_rejects_missing_origin(
    app, client: AsyncClient, session_factory,
):
    """Server-to-server calls with no Origin can't be browser embeds."""
    org_id = await create_org(session_factory)
    key = configure_website(app, org_id=org_id)
    resp = await client.post(
        "/v1/website/sessions",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 400
    assert "origin" in resp.json()["detail"].lower()


async def test_bootstrap_rejects_origin_not_in_allow_list(
    app, client: AsyncClient, session_factory,
):
    """A valid key used from an unlisted origin returns 403."""
    org_id = await create_org(session_factory)
    key = configure_website(app, org_id=org_id, allowed_origins=_DEFAULT_ORIGIN)
    resp = await client.post(
        "/v1/website/sessions",
        headers={
            "Authorization": f"Bearer {key}",
            "Origin": "https://evil.com",
        },
    )
    assert resp.status_code == 403
    assert "allow-list" in resp.json()["detail"].lower()


async def test_bootstrap_rejects_unknown_publishable_key(
    app, client: AsyncClient, session_factory,
):
    org_id = await create_org(session_factory)
    configure_website(app, org_id=org_id)  # mints a real key
    # Present a syntactically-valid but wrong key.
    resp = await client.post(
        "/v1/website/sessions",
        headers={
            "Authorization": "Bearer surg_wk_bogus-key-nonexistent",
            "Origin": _DEFAULT_ORIGIN,
        },
    )
    assert resp.status_code == 401


async def test_bootstrap_rejects_non_publishable_prefix(
    app, client: AsyncClient, session_factory,
):
    """A service-account key is the wrong token shape for this endpoint."""
    org_id = await create_org(session_factory)
    configure_website(app, org_id=org_id)
    resp = await client.post(
        "/v1/website/sessions",
        headers={
            "Authorization": "Bearer surg_sk_wrong_token_kind",
            "Origin": _DEFAULT_ORIGIN,
        },
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Message endpoint — cookie + CSRF + origin binding
# ---------------------------------------------------------------------------


async def _bootstrap(
    app, client: AsyncClient, session_factory,
    *,
    allowed_origins: str = _DEFAULT_ORIGIN,
) -> tuple[str, UUID, str]:
    """Bootstrap a session; return (csrf_token, session_id, origin).

    Per-test fresh org so test runs don't share state.  The origin we
    bootstrap from is the first entry of ``allowed_origins`` so the
    cookie binding lines up with what the message tests will send.
    """
    org_id = await create_org(session_factory)
    key = configure_website(
        app, org_id=org_id, allowed_origins=allowed_origins,
    )
    origin = allowed_origins.split(",")[0].strip()
    resp = await client.post(
        "/v1/website/sessions",
        headers={"Authorization": f"Bearer {key}", "Origin": origin},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["csrf_token"], UUID(body["session_id"]), origin


async def test_send_message_happy_path(
    app, client: AsyncClient, session_factory, session_store,
):
    csrf, sid, origin = await _bootstrap(app, client, session_factory)
    resp = await client.post(
        f"/v1/website/sessions/{sid}/messages",
        json={"content": "hello"},
        headers={"Origin": origin, CSRF_HEADER_NAME: csrf},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["event_id"] > 0

    events = await session_store.get_events(sid)
    user_msgs = [e for e in events if e.type == "user.message"]
    assert len(user_msgs) == 1
    assert user_msgs[0].data["content"] == "hello"


async def test_send_message_rejects_missing_cookie(
    app, client: AsyncClient, session_factory,
):
    """No cookie → 401 without touching the session store."""
    org_id = await create_org(session_factory)
    configure_website(app, org_id=org_id)
    # Bootstrap then drop the cookie before the message call.
    async with AsyncClient(
        transport=client._transport, base_url="https://test",
    ) as fresh:
        sid = uuid.uuid4()
        resp = await fresh.post(
            f"/v1/website/sessions/{sid}/messages",
            json={"content": "hi"},
            headers={"Origin": _DEFAULT_ORIGIN, CSRF_HEADER_NAME: "anything"},
        )
    assert resp.status_code == 401


async def test_send_message_rejects_missing_csrf(
    app, client: AsyncClient, session_factory,
):
    """Double-submit CSRF requires both cookie + header tokens."""
    _, sid, origin = await _bootstrap(app, client, session_factory)
    resp = await client.post(
        f"/v1/website/sessions/{sid}/messages",
        json={"content": "hi"},
        headers={"Origin": origin},
    )
    assert resp.status_code == 403
    assert "csrf" in resp.json()["detail"].lower()


async def test_send_message_rejects_wrong_csrf(
    app, client: AsyncClient, session_factory,
):
    _, sid, origin = await _bootstrap(app, client, session_factory)
    resp = await client.post(
        f"/v1/website/sessions/{sid}/messages",
        json={"content": "hi"},
        headers={"Origin": origin, CSRF_HEADER_NAME: "not-the-real-token"},
    )
    assert resp.status_code == 403


async def test_send_message_rejects_wrong_origin(
    app, client: AsyncClient, session_factory,
):
    """Cookie from customer.com replayed with attacker.com Origin → 403."""
    csrf, sid, _origin = await _bootstrap(app, client, session_factory)
    resp = await client.post(
        f"/v1/website/sessions/{sid}/messages",
        json={"content": "hi"},
        headers={"Origin": "https://attacker.com", CSRF_HEADER_NAME: csrf},
    )
    assert resp.status_code == 403


async def test_send_message_rejects_mismatched_session_id(
    app, client: AsyncClient, session_factory,
):
    """Cookie session X replayed against URL session Y → 404."""
    csrf, _sid, origin = await _bootstrap(app, client, session_factory)
    other_sid = uuid.uuid4()
    resp = await client.post(
        f"/v1/website/sessions/{other_sid}/messages",
        json={"content": "hi"},
        headers={"Origin": origin, CSRF_HEADER_NAME: csrf},
    )
    assert resp.status_code == 404


async def test_session_message_cap_returns_429(
    app, client: AsyncClient, session_factory,
):
    """Cap is read from session.config.session_message_cap set at bootstrap."""
    org_id = await create_org(session_factory)
    key = configure_website(app, org_id=org_id)
    app.state.settings.website.session_message_cap = 1

    resp = await client.post(
        "/v1/website/sessions",
        headers={"Authorization": f"Bearer {key}", "Origin": _DEFAULT_ORIGIN},
    )
    assert resp.status_code == 201, resp.text
    csrf = resp.json()["csrf_token"]
    sid = UUID(resp.json()["session_id"])

    ok = await client.post(
        f"/v1/website/sessions/{sid}/messages",
        json={"content": "first"},
        headers={"Origin": _DEFAULT_ORIGIN, CSRF_HEADER_NAME: csrf},
    )
    assert ok.status_code == 202

    # Messages don't bump message_count synchronously without a worker,
    # but the cap logic reads session.message_count.  Manually bump it
    # to simulate the worker having processed the first message.
    async with session_factory() as db:
        from sqlalchemy import text as _text
        await db.execute(
            _text("UPDATE sessions SET message_count = :c WHERE id = :id"),
            {"c": 1, "id": sid},
        )
        await db.commit()

    capped = await client.post(
        f"/v1/website/sessions/{sid}/messages",
        json={"content": "second"},
        headers={"Origin": _DEFAULT_ORIGIN, CSRF_HEADER_NAME: csrf},
    )
    assert capped.status_code == 429


async def test_disabling_channel_blocks_existing_sessions(
    app, client: AsyncClient, session_factory,
):
    """An operator flipping website.enabled=false blocks in-flight visitors."""
    csrf, sid, origin = await _bootstrap(app, client, session_factory)
    # Flip the deployment-level feature flag mid-session.
    app.state.settings.website.enabled = False
    denied = await client.post(
        f"/v1/website/sessions/{sid}/messages",
        json={"content": "hi"},
        headers={"Origin": origin, CSRF_HEADER_NAME: csrf},
    )
    assert denied.status_code == 404


# ---------------------------------------------------------------------------
# CORS preflight
# ---------------------------------------------------------------------------


async def test_preflight_permissive_regardless_of_config(
    app, client: AsyncClient, session_factory,
):
    """Preflight cannot carry credentials, so it must always succeed.

    Actual authorization happens on the follow-up request.
    """
    org_id = await create_org(session_factory)
    configure_website(app, org_id=org_id)
    resp = await client.request(
        "OPTIONS",
        "/v1/website/sessions",
        headers={
            "Origin": "https://any-origin.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert resp.status_code == 204
    assert resp.headers["access-control-allow-origin"] == "https://any-origin.com"
    assert resp.headers["access-control-allow-credentials"] == "true"
    assert "POST" in resp.headers["access-control-allow-methods"]
    assert CSRF_HEADER_NAME.lower() in resp.headers[
        "access-control-allow-headers"
    ].lower()


async def test_actual_response_echoes_origin(
    app, client: AsyncClient, session_factory,
):
    """The per-path CORS middleware sets ACAO to the request Origin."""
    org_id = await create_org(session_factory)
    key = configure_website(app, org_id=org_id)
    resp = await client.post(
        "/v1/website/sessions",
        headers={
            "Authorization": f"Bearer {key}",
            "Origin": _DEFAULT_ORIGIN,
        },
    )
    assert resp.status_code == 201
    assert resp.headers["access-control-allow-origin"] == _DEFAULT_ORIGIN
    assert resp.headers["access-control-allow-credentials"] == "true"


# ---------------------------------------------------------------------------
# End session
# ---------------------------------------------------------------------------


async def test_end_session_completes_and_clears_cookie(
    app, client: AsyncClient, session_factory, session_store,
):
    csrf, sid, origin = await _bootstrap(app, client, session_factory)
    resp = await client.post(
        f"/v1/website/sessions/{sid}/end",
        headers={"Origin": origin, CSRF_HEADER_NAME: csrf},
    )
    assert resp.status_code == 204
    set_cookie = resp.headers.get("set-cookie", "")
    # Delete cookies set Max-Age=0 or Expires=1970
    assert ("Max-Age=0" in set_cookie) or ("1970" in set_cookie)

    session = await session_store.get_session(sid)
    assert session.status == "completed"


# ---------------------------------------------------------------------------
# Website channel auth does NOT leak onto other paths
# ---------------------------------------------------------------------------


async def test_bootstrap_works_through_api_prefix(
    app, client: AsyncClient, session_factory,
):
    """Bootstrap at ``/api/v1/website/sessions`` sets a cookie whose Path
    allows the browser to send it back on the same URL form.
    """
    org_id = await create_org(session_factory)
    key = configure_website(app, org_id=org_id)
    resp = await client.post(
        "/api/v1/website/sessions",
        headers={
            "Authorization": f"Bearer {key}",
            "Origin": _DEFAULT_ORIGIN,
        },
    )
    assert resp.status_code == 201, resp.text
    set_cookie_raw = resp.headers.get("set-cookie", "")
    assert "Path=/" in set_cookie_raw
    sid = UUID(resp.json()["session_id"])
    csrf = resp.json()["csrf_token"]
    follow = await client.post(
        f"/api/v1/website/sessions/{sid}/messages",
        json={"content": "hi"},
        headers={"Origin": _DEFAULT_ORIGIN, CSRF_HEADER_NAME: csrf},
    )
    assert follow.status_code == 202


async def test_publishable_key_rejected_outside_website_prefix(
    app, client: AsyncClient, session_factory,
):
    """``surg_wk_…`` tokens must not authenticate any non-website path."""
    org_id = await create_org(session_factory)
    key = configure_website(app, org_id=org_id)
    resp = await client.get(
        "/v1/sessions",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# SSE stream — terminal-status race guard
# ---------------------------------------------------------------------------


async def _read_sse_events(response, *, until_types: set[str], deadline_s: float):
    """Consume an SSE response and return ``(event, data)`` pairs.

    Returns once **any** event in ``until_types`` has been observed, or
    when ``deadline_s`` elapses (so a hung handler surfaces as a test
    failure rather than blocking forever).
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
                continue
            if line.startswith("event:"):
                event_type = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].lstrip())

    try:
        await asyncio.wait_for(_consume(), timeout=deadline_s)
    except asyncio.TimeoutError:
        pass
    return received


async def test_stream_terminal_close_emits_session_done(
    app, client: AsyncClient, session_factory, session_store,
):
    """A truly terminal website session closes with ``session.done``.

    Without a pending POST, the handler's grace window times out and
    the stream sends a single ``session.done`` event and closes. The
    cookie set by bootstrap carries the website-session JWT.
    """
    csrf, sid, origin = await _bootstrap(app, client, session_factory)
    await app.state.session_store.update_session_status(sid, "completed")

    async with client.stream(
        "GET",
        f"/v1/website/sessions/{sid}/events?after=0",
        headers={"Origin": origin},
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


async def test_stream_race_recovery_streams_resumed_events(
    app, client: AsyncClient, session_factory,
):
    """Race guard delivers events that land during the grace window.

    Mirrors the production race: SSE opens on a ``completed`` session,
    then a SESSION_RESUME publish lands during the grace window. The
    handler must wake on the publish, re-check status, find ``active``,
    and stream through SESSION_RESUME + USER_MESSAGE without ever
    emitting ``session.done``.

    Uses ``app.state.session_store`` (redis-aware) so ``emit_event``
    actually publishes on ``surogates:session:{id}``.
    """
    csrf, sid, origin = await _bootstrap(app, client, session_factory)
    redis_store: SessionStore = app.state.session_store
    await redis_store.update_session_status(sid, "completed")

    async def _flip_to_active_after_subscribe():
        # Give the SSE handler time to enter event_generator and subscribe
        # to ``surogates:session:{id}``. The subscribe must precede the
        # publish — that's the whole point of the race guard.
        await asyncio.sleep(0.25)
        await redis_store.update_session_status(sid, "active")
        await redis_store.emit_event(sid, EventType.SESSION_RESUME, {})
        await redis_store.emit_event(
            sid, EventType.USER_MESSAGE, {"content": "are you there?"},
        )

    flipper = asyncio.create_task(_flip_to_active_after_subscribe())
    try:
        async with client.stream(
            "GET",
            f"/v1/website/sessions/{sid}/events?after=0",
            headers={"Origin": origin},
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
