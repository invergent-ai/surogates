"""Integration tests for the public-website channel.

Covers the bootstrap path (publishable key + Origin → signed session
cookie + CSRF token), the cookie-authenticated message endpoint with
CSRF double-submit, per-agent origin enforcement, SSE streaming, and
the session tool allow-list that stops a visitor from calling anything
outside the agent's configured subset.
"""

from __future__ import annotations

import json
import os
import uuid
from uuid import UUID

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from surogates.channels.website_agent_store import (
    WebsiteAgentStore,
    _reset_caches as _reset_website_caches,
)
from surogates.channels.website_session import (
    COOKIE_NAME,
    CSRF_HEADER_NAME,
)
from surogates.session.store import SessionStore
from surogates.tenant.credentials import CredentialVault

from .conftest import create_org

pytestmark = pytest.mark.asyncio(loop_scope="session")


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


@pytest_asyncio.fixture(autouse=True)
async def _flush_website_caches():
    """Clear the publishable-key cache between tests.

    Several tests mutate agent rows in ways that the in-process cache
    would otherwise mask -- ``update(allowed_origins=...)`` and
    ``delete`` both invalidate their own cache entries, but a fresh
    test starting with a stale cache from a prior test would see the
    old row.  Resetting at the boundary makes the tests order-independent.
    """
    _reset_website_caches()
    yield
    _reset_website_caches()


async def _create_agent(
    session_factory,
    *,
    allowed_origins: list[str] | None = None,
    tool_allow_list: list[str] | None = None,
    enabled: bool = True,
    name: str = "support-bot",
    session_message_cap: int = 0,
):
    """Create an org + website agent; return (org_id, agent_id, raw_key)."""
    org_id = await create_org(session_factory)
    issued = await WebsiteAgentStore(session_factory).create(
        org_id=org_id,
        name=name,
        allowed_origins=allowed_origins or ["https://customer.com"],
        tool_allow_list=tool_allow_list or [],
        session_message_cap=session_message_cap,
    )
    if not enabled:
        await WebsiteAgentStore(session_factory).update(
            issued.id, enabled=False,
        )
    return org_id, issued.id, issued.publishable_key


# ---------------------------------------------------------------------------
# Publishable key + origin enforcement at bootstrap
# ---------------------------------------------------------------------------


async def test_bootstrap_happy_path_creates_session_and_cookie(
    client: AsyncClient, session_factory, session_store,
):
    """Valid publishable key + allowed origin → 201 with session cookie + CSRF."""
    org_id, agent_id, key = await _create_agent(session_factory)

    resp = await client.post(
        "/v1/website/sessions",
        headers={
            "Authorization": f"Bearer {key}",
            "Origin": "https://customer.com",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["agent_name"] == "support-bot"
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

    # Session persisted with channel='website', no user, website config.
    session = await session_store.get_session(UUID(body["session_id"]))
    assert session.channel == "website"
    assert session.user_id is None
    assert session.org_id == org_id
    assert session.config["website_agent_id"] == str(agent_id)
    assert session.config["website_origin"] == "https://customer.com"


async def test_bootstrap_rejects_missing_origin(
    client: AsyncClient, session_factory,
):
    """Server-to-server calls with no Origin can't be browser embeds."""
    _, _, key = await _create_agent(session_factory)
    resp = await client.post(
        "/v1/website/sessions",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 400
    assert "origin" in resp.json()["detail"].lower()


async def test_bootstrap_rejects_origin_not_in_allow_list(
    client: AsyncClient, session_factory,
):
    """A valid key used from an unlisted origin returns 403."""
    _, _, key = await _create_agent(
        session_factory, allowed_origins=["https://customer.com"],
    )
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
    client: AsyncClient, session_factory,
):
    # Fabricate a syntactically valid but unregistered key.
    resp = await client.post(
        "/v1/website/sessions",
        headers={
            "Authorization": "Bearer surg_wk_bogus-key-nonexistent",
            "Origin": "https://customer.com",
        },
    )
    assert resp.status_code == 401


async def test_bootstrap_rejects_non_publishable_prefix(
    client: AsyncClient, session_factory,
):
    """A service-account key is the wrong token shape for this endpoint."""
    await _create_agent(session_factory)
    resp = await client.post(
        "/v1/website/sessions",
        headers={
            "Authorization": "Bearer surg_sk_wrong_token_kind",
            "Origin": "https://customer.com",
        },
    )
    assert resp.status_code == 401


async def test_bootstrap_rejects_disabled_agent(
    client: AsyncClient, session_factory,
):
    _, _, key = await _create_agent(session_factory, enabled=False)
    resp = await client.post(
        "/v1/website/sessions",
        headers={
            "Authorization": f"Bearer {key}",
            "Origin": "https://customer.com",
        },
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Message endpoint — cookie + CSRF + origin binding
# ---------------------------------------------------------------------------


async def _bootstrap(
    client: AsyncClient, session_factory, **agent_kwargs,
) -> tuple[str, str, UUID, str]:
    """Bootstrap a session; return (key, csrf, session_id, origin)."""
    _, _, key = await _create_agent(session_factory, **agent_kwargs)
    origin = (
        agent_kwargs.get("allowed_origins", ["https://customer.com"])[0]
    )
    resp = await client.post(
        "/v1/website/sessions",
        headers={"Authorization": f"Bearer {key}", "Origin": origin},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return key, body["csrf_token"], UUID(body["session_id"]), origin


async def test_send_message_happy_path(
    client: AsyncClient, session_factory, session_store,
):
    _, csrf, sid, origin = await _bootstrap(client, session_factory)
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
    client: AsyncClient, session_factory,
):
    """No cookie → 401 without touching the session store."""
    _, _, key = await _create_agent(session_factory)
    # Bootstrap then drop the cookie before the message call.
    async with AsyncClient(
        transport=client._transport, base_url="https://test",
    ) as fresh:
        # Manually craft a session id that won't be reached.
        sid = uuid.uuid4()
        resp = await fresh.post(
            f"/v1/website/sessions/{sid}/messages",
            json={"content": "hi"},
            headers={"Origin": "https://customer.com",
                     CSRF_HEADER_NAME: "anything"},
        )
    assert resp.status_code == 401


async def test_send_message_rejects_missing_csrf(
    client: AsyncClient, session_factory,
):
    """Double-submit CSRF requires both cookie + header tokens."""
    _, csrf, sid, origin = await _bootstrap(client, session_factory)
    # Same client so cookie is present; just skip the CSRF header.
    resp = await client.post(
        f"/v1/website/sessions/{sid}/messages",
        json={"content": "hi"},
        headers={"Origin": origin},
    )
    assert resp.status_code == 403
    assert "csrf" in resp.json()["detail"].lower()


async def test_send_message_rejects_wrong_csrf(
    client: AsyncClient, session_factory,
):
    _, csrf, sid, origin = await _bootstrap(client, session_factory)
    resp = await client.post(
        f"/v1/website/sessions/{sid}/messages",
        json={"content": "hi"},
        headers={"Origin": origin, CSRF_HEADER_NAME: "not-the-real-token"},
    )
    assert resp.status_code == 403


async def test_send_message_rejects_wrong_origin(
    client: AsyncClient, session_factory,
):
    """Cookie from customer.com replayed with attacker.com Origin → 403."""
    _, csrf, sid, _origin = await _bootstrap(client, session_factory)
    resp = await client.post(
        f"/v1/website/sessions/{sid}/messages",
        json={"content": "hi"},
        headers={"Origin": "https://attacker.com",
                 CSRF_HEADER_NAME: csrf},
    )
    assert resp.status_code == 403


async def test_send_message_rejects_mismatched_session_id(
    client: AsyncClient, session_factory,
):
    """Cookie session X replayed against URL session Y → 404.

    The session JWT binds to exactly one session_id, so changing the
    path is a cross-session attempt that we refuse to distinguish from
    "session doesn't exist".
    """
    _, csrf, _sid, origin = await _bootstrap(client, session_factory)
    other_sid = uuid.uuid4()
    resp = await client.post(
        f"/v1/website/sessions/{other_sid}/messages",
        json={"content": "hi"},
        headers={"Origin": origin, CSRF_HEADER_NAME: csrf},
    )
    assert resp.status_code == 404


async def test_session_message_cap_returns_429(
    client: AsyncClient, session_factory,
):
    """Cap is read from session.config.session_message_cap set at bootstrap."""
    _, csrf, sid, origin = await _bootstrap(
        client, session_factory, session_message_cap=1,
    )
    ok = await client.post(
        f"/v1/website/sessions/{sid}/messages",
        json={"content": "first"},
        headers={"Origin": origin, CSRF_HEADER_NAME: csrf},
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
        headers={"Origin": origin, CSRF_HEADER_NAME: csrf},
    )
    assert capped.status_code == 429


# ---------------------------------------------------------------------------
# Disabled agent blocks in-flight sessions too
# ---------------------------------------------------------------------------


async def test_disabling_agent_blocks_existing_sessions(
    client: AsyncClient, session_factory,
):
    """An ops operator disabling the agent must stop in-flight visitors."""
    _, agent_id, key = await _create_agent(session_factory)
    resp = await client.post(
        "/v1/website/sessions",
        headers={"Authorization": f"Bearer {key}",
                 "Origin": "https://customer.com"},
    )
    sid = UUID(resp.json()["session_id"])
    csrf = resp.json()["csrf_token"]

    # Disable the agent row created for this test.
    await WebsiteAgentStore(session_factory).update(agent_id, enabled=False)

    denied = await client.post(
        f"/v1/website/sessions/{sid}/messages",
        json={"content": "hi"},
        headers={"Origin": "https://customer.com",
                 CSRF_HEADER_NAME: csrf},
    )
    assert denied.status_code == 403


# ---------------------------------------------------------------------------
# CORS preflight
# ---------------------------------------------------------------------------


async def test_preflight_permissive_regardless_of_agent(
    client: AsyncClient, session_factory,
):
    """Preflight cannot carry credentials, so it must always succeed.

    Actual authorization happens on the follow-up request.
    """
    await _create_agent(session_factory)
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
    client: AsyncClient, session_factory,
):
    """The per-agent CORS middleware sets ACAO to the request Origin."""
    _, _, key = await _create_agent(session_factory)
    resp = await client.post(
        "/v1/website/sessions",
        headers={
            "Authorization": f"Bearer {key}",
            "Origin": "https://customer.com",
        },
    )
    assert resp.status_code == 201
    assert resp.headers["access-control-allow-origin"] == "https://customer.com"
    assert resp.headers["access-control-allow-credentials"] == "true"


# ---------------------------------------------------------------------------
# Tool allow-list enforcement
# ---------------------------------------------------------------------------


async def test_allow_list_materialized_onto_session_config(
    client: AsyncClient, session_factory, session_store,
):
    """Bootstrap copies the agent's tool_allow_list onto session.config.

    The harness/tool_exec path reads from session.config rather than
    the agent row at dispatch time, so edits to the agent later don't
    retroactively widen a running session's permissions.
    """
    _, _, key = await _create_agent(
        session_factory,
        tool_allow_list=["web_search", "clarify"],
    )
    resp = await client.post(
        "/v1/website/sessions",
        headers={"Authorization": f"Bearer {key}",
                 "Origin": "https://customer.com"},
    )
    sid = UUID(resp.json()["session_id"])
    session = await session_store.get_session(sid)
    assert session.config["tool_allow_list"] == ["web_search", "clarify"]


async def test_allow_list_blocks_tool_outside_subset_at_harness(
    session_store, session_factory,
):
    """execute_single_tool emits policy.denied + tool.result for non-allowlisted tools.

    Exercises the allow-list check we added to ``tool_exec.py``.  The
    website bootstrap writes the list onto ``session.config``; this
    test mimics that config and verifies the harness enforces it, so a
    visitor cannot slip an ``execute_code`` call past governance even
    if the LLM decides to try.
    """
    from dataclasses import dataclass
    from sqlalchemy import text as _text

    from surogates.harness.tool_exec import execute_single_tool
    from surogates.tools.registry import ToolRegistry
    from .conftest import create_user

    @dataclass
    class _Stub:
        org_id: UUID
        user_id: UUID

    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
        channel="website",
        config={
            "workspace_path": "/workspace",
            "tool_allow_list": ["web_search", "clarify"],
        },
    )
    lease = await session_store.try_acquire_lease(
        session.id, "worker-allow-test", ttl_seconds=60,
    )
    assert lease is not None

    # execute_code is not in the allow-list.
    tc = {
        "id": "tc-1",
        "function": {
            "name": "execute_code",
            "arguments": json.dumps({"code": "print('hi')"}),
        },
    }
    result_msg = await execute_single_tool(
        tc,
        session=session,
        lease=lease,
        store=session_store,
        tools=ToolRegistry(),
        tenant=_Stub(org_id=org_id, user_id=user_id),
    )
    # Tool result carries the blocked reason and references execute_code.
    assert "execute_code" in result_msg["content"]
    assert "allow-list" in result_msg["content"]

    async with session_factory() as db:
        row = (
            await db.execute(
                _text(
                    "SELECT data FROM events "
                    "WHERE session_id = :sid AND type = 'policy.denied' "
                    "ORDER BY id DESC LIMIT 1"
                ),
                {"sid": session.id},
            )
        ).mappings().one()
    assert row["data"]["tool"] == "execute_code"
    assert "allow-list" in row["data"]["reason"]


# ---------------------------------------------------------------------------
# End session
# ---------------------------------------------------------------------------


async def test_end_session_completes_and_clears_cookie(
    client: AsyncClient, session_factory, session_store,
):
    _, csrf, sid, origin = await _bootstrap(client, session_factory)
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
    client: AsyncClient, session_factory,
):
    """Bootstrap at ``/api/v1/website/sessions`` sets a cookie whose Path
    allows the browser to send it back on the same URL form.

    ``StripApiPrefixMiddleware`` rewrites ``/api/v1/...`` to ``/v1/...``
    server-side, but the browser sees and scopes cookies by the URL it
    addressed.  A cookie pinned to ``Path=/v1/website`` would not be
    returned on a follow-up to ``/api/v1/website/...``; ``Path=/``
    works for both deployment shapes.
    """
    _, _, key = await _create_agent(session_factory)
    resp = await client.post(
        "/api/v1/website/sessions",
        headers={
            "Authorization": f"Bearer {key}",
            "Origin": "https://customer.com",
        },
    )
    assert resp.status_code == 201, resp.text
    set_cookie_raw = resp.headers.get("set-cookie", "")
    # Path=/ is what lets the cookie ride back on either form of the URL.
    assert "Path=/" in set_cookie_raw
    # Follow-up on the prefixed form must see the cookie.
    sid = UUID(resp.json()["session_id"])
    csrf = resp.json()["csrf_token"]
    follow = await client.post(
        f"/api/v1/website/sessions/{sid}/messages",
        json={"content": "hi"},
        headers={"Origin": "https://customer.com", CSRF_HEADER_NAME: csrf},
    )
    assert follow.status_code == 202


async def test_publishable_key_rejected_outside_website_prefix(
    client: AsyncClient, session_factory,
):
    """``surg_wk_…`` tokens must not authenticate any non-website path.

    The global auth middleware treats ``/v1/website/*`` as public (the
    routes self-authenticate); any other path still runs JWT/SA auth,
    which rejects the wrong token shape.
    """
    _, _, key = await _create_agent(session_factory)
    resp = await client.get(
        "/v1/sessions",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 401
