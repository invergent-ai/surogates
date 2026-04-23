"""Integration tests for the FastAPI application against real PostgreSQL + Redis."""

from __future__ import annotations

import os
import uuid
from uuid import UUID

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from surogates.session.store import SessionStore
from surogates.tenant.auth.jwt import create_access_token
from surogates.tenant.credentials import CredentialVault

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def app(session_factory, redis_client, pg_url, redis_url):
    """Build a FastAPI application wired to the test containers."""
    # Set env vars so load_settings() picks up the right URLs
    os.environ["SUROGATES_DB_URL"] = pg_url
    os.environ["SUROGATES_REDIS_URL"] = redis_url

    from surogates.api.app import create_app
    from surogates.config import Settings

    application = create_app()

    # Override state with test-container-backed objects so middleware and
    # route handlers use the test database instead of whatever lifespan
    # created.
    application.state.session_factory = session_factory
    application.state.redis = redis_client
    application.state.session_store = SessionStore(session_factory)
    application.state.settings = Settings()

    from surogates.storage.backend import create_backend
    application.state.storage = create_backend(application.state.settings)

    # Vault for /v1/admin/credentials and MCP credential refs.
    application.state.credential_vault = CredentialVault(
        session_factory, Fernet.generate_key()
    )

    return application


@pytest_asyncio.fixture(loop_scope="session")
async def client(app):
    """Async HTTP client bound to the test app."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _create_test_tenant(
    session_factory,
    *,
    password: str = "testpass123",
    permissions: set[str] | None = None,
) -> tuple[UUID, UUID, str, str]:
    """Create org + user + JWT token. Returns (org_id, user_id, token, email)."""
    org_id = await create_org(session_factory)
    user_id = uuid.uuid4()
    email = f"user-{user_id}@test.com"
    await create_user(
        session_factory, org_id, user_id=user_id, email=email, password=password
    )

    perms = permissions or {"sessions:read", "sessions:write", "tools:read", "admin"}
    token = create_access_token(org_id, user_id, perms)
    return org_id, user_id, token, email


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


async def test_health(client: AsyncClient):
    """GET /health returns 200."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_health_ready(client: AsyncClient):
    """GET /health/ready returns 200 when DB + Redis are reachable."""
    resp = await client.get("/health/ready")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["checks"]["database"] == "ok"
    assert data["checks"]["redis"] == "ok"


# ---------------------------------------------------------------------------
# Admin -- Orgs and Users
# ---------------------------------------------------------------------------


async def test_admin_create_org(client: AsyncClient, session_factory):
    """POST /v1/admin/orgs creates an org."""
    _, _, token, _ = await _create_test_tenant(session_factory)
    resp = await client.post(
        "/v1/admin/orgs",
        json={"name": "New Org"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "New Org"
    assert "id" in data


async def test_admin_create_user(client: AsyncClient, session_factory):
    """POST /v1/admin/orgs/{id}/users creates a user within an org."""
    org_id, _, token, _ = await _create_test_tenant(session_factory)
    resp = await client.post(
        f"/v1/admin/orgs/{org_id}/users",
        json={
            "email": "newuser@test.com",
            "display_name": "New User",
            "password": "secret123",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["email"] == "newuser@test.com"
    assert data["display_name"] == "New User"


# ---------------------------------------------------------------------------
# Auth -- Login and Me
# ---------------------------------------------------------------------------


async def test_auth_login(client: AsyncClient, session_factory):
    """POST /v1/auth/login returns JWT tokens."""
    org_id, user_id, _, email = await _create_test_tenant(
        session_factory, password="mypassword"
    )

    resp = await client.post(
        "/v1/auth/login",
        json={"email": email, "password": "mypassword", "org_id": str(org_id)},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "bearer"


async def test_auth_me(client: AsyncClient, session_factory):
    """GET /v1/auth/me returns user info for the authenticated user."""
    org_id, user_id, token, email = await _create_test_tenant(session_factory)

    resp = await client.get(
        "/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(user_id)
    assert data["org_id"] == str(org_id)
    assert data["email"] == email


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


async def test_create_session(client: AsyncClient, session_factory):
    """POST /v1/sessions creates a session."""
    _, _, token, _ = await _create_test_tenant(session_factory)

    resp = await client.post(
        "/v1/sessions",
        json={"model": "gpt-4o"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "active"
    assert data["channel"] == "web"
    assert "id" in data


async def test_send_message(client: AsyncClient, session_factory):
    """POST /v1/sessions/{id}/messages returns 202 and enqueues."""
    _, _, token, _ = await _create_test_tenant(session_factory)

    # Create session first
    create_resp = await client.post(
        "/v1/sessions",
        json={"model": "gpt-4o"},
        headers={"Authorization": f"Bearer {token}"},
    )
    session_id = create_resp.json()["id"]

    # Send message
    resp = await client.post(
        f"/v1/sessions/{session_id}/messages",
        json={"content": "Hello agent!"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["event_id"] > 0
    assert data["status"] == "processing"


async def test_get_session(client: AsyncClient, session_factory):
    """GET /v1/sessions/{id} returns session details."""
    _, _, token, _ = await _create_test_tenant(session_factory)

    create_resp = await client.post(
        "/v1/sessions",
        json={"model": "gpt-4o"},
        headers={"Authorization": f"Bearer {token}"},
    )
    session_id = create_resp.json()["id"]

    resp = await client.get(
        f"/v1/sessions/{session_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == session_id
    assert data["status"] == "active"


async def test_list_sessions(client: AsyncClient, session_factory):
    """GET /v1/sessions returns a paginated list."""
    _, _, token, _ = await _create_test_tenant(session_factory)

    # Create 2 sessions
    for _ in range(2):
        await client.post(
            "/v1/sessions",
            json={"model": "gpt-4o"},
            headers={"Authorization": f"Bearer {token}"},
        )

    resp = await client.get(
        "/v1/sessions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["sessions"]) >= 2
    assert data["total"] >= 2


async def test_poll_events(client: AsyncClient, session_factory):
    """GET /v1/sessions/{id}/events/poll returns events."""
    _, _, token, _ = await _create_test_tenant(session_factory)

    create_resp = await client.post(
        "/v1/sessions",
        json={"model": "gpt-4o"},
        headers={"Authorization": f"Bearer {token}"},
    )
    session_id = create_resp.json()["id"]

    # Send a message to create an event
    await client.post(
        f"/v1/sessions/{session_id}/messages",
        json={"content": "test poll"},
        headers={"Authorization": f"Bearer {token}"},
    )

    resp = await client.get(
        f"/v1/sessions/{session_id}/events/poll",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["events"]) >= 1
    assert data["events"][0]["type"] == "user.message"


async def test_pause_resume(client: AsyncClient, session_factory):
    """POST pause then resume transitions session status correctly."""
    _, _, token, _ = await _create_test_tenant(session_factory)

    create_resp = await client.post(
        "/v1/sessions",
        json={"model": "gpt-4o"},
        headers={"Authorization": f"Bearer {token}"},
    )
    session_id = create_resp.json()["id"]

    # Pause
    pause_resp = await client.post(
        f"/v1/sessions/{session_id}/pause",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert pause_resp.status_code == 200
    assert pause_resp.json()["status"] == "paused"

    # Resume
    resume_resp = await client.post(
        f"/v1/sessions/{session_id}/resume",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resume_resp.status_code == 200
    assert resume_resp.json()["status"] == "active"


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


def _with_agent_id(app, agent_id: str):
    """Context manager that pins ``app.state.settings.agent_id`` for a test.

    The shared ``app`` fixture defaults to an empty agent_id, but retry
    (and resume) re-enqueue via :func:`agent_queue_key` which rejects
    the empty string.  Tests that exercise the full retry path set a
    non-empty value for the duration of the test.
    """
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        original = app.state.settings.agent_id
        app.state.settings.agent_id = agent_id
        try:
            yield
        finally:
            app.state.settings.agent_id = original

    return _ctx()


async def test_retry_failed_session(client: AsyncClient, session_factory, app):
    """POST /retry on a failed session re-enqueues it and flips to active."""
    with _with_agent_id(app, "test-agent-retry-1"):
        _, _, token, _ = await _create_test_tenant(session_factory)

        create_resp = await client.post(
            "/v1/sessions",
            json={"model": "gpt-4o"},
            headers={"Authorization": f"Bearer {token}"},
        )
        session_id = create_resp.json()["id"]

        # Force session into the 'failed' state.
        store: SessionStore = app.state.session_store
        await store.update_session_status(UUID(session_id), "failed")

        # Drain any pre-existing queue entries so we can assert the retry enqueue.
        from surogates.config import agent_queue_key
        queue_key = agent_queue_key("test-agent-retry-1")
        await app.state.redis.delete(queue_key)

        retry_resp = await client.post(
            f"/v1/sessions/{session_id}/retry",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert retry_resp.status_code == 200
        assert retry_resp.json()["status"] == "active"

        # A SESSION_RESUME event with source='user_retry' was emitted.
        events = await store.get_events(UUID(session_id))
        resume_events = [e for e in events if e.type == "session.resume"]
        assert resume_events, "expected a session.resume event"
        assert resume_events[-1].data.get("source") == "user_retry"

        # Redis queue contains the session id.
        queued = await app.state.redis.zrange(queue_key, 0, -1)
        assert any(
            (member.decode() if isinstance(member, bytes) else member) == session_id
            for member in queued
        ), "expected session to be re-enqueued"


async def test_retry_paused_session(client: AsyncClient, session_factory, app):
    """Retry also works for paused sessions (same semantics as /resume)."""
    with _with_agent_id(app, "test-agent-retry-2"):
        _, _, token, _ = await _create_test_tenant(session_factory)

        create_resp = await client.post(
            "/v1/sessions",
            json={"model": "gpt-4o"},
            headers={"Authorization": f"Bearer {token}"},
        )
        session_id = create_resp.json()["id"]

        await client.post(
            f"/v1/sessions/{session_id}/pause",
            headers={"Authorization": f"Bearer {token}"},
        )

        retry_resp = await client.post(
            f"/v1/sessions/{session_id}/retry",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert retry_resp.status_code == 200
        assert retry_resp.json()["status"] == "active"


async def test_retry_active_session_409(client: AsyncClient, session_factory):
    """Retry of an active session returns 409 Conflict."""
    _, _, token, _ = await _create_test_tenant(session_factory)

    create_resp = await client.post(
        "/v1/sessions",
        json={"model": "gpt-4o"},
        headers={"Authorization": f"Bearer {token}"},
    )
    session_id = create_resp.json()["id"]

    retry_resp = await client.post(
        f"/v1/sessions/{session_id}/retry",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert retry_resp.status_code == 409


async def test_retry_nonexistent_session_404(client: AsyncClient, session_factory):
    """Retry of an unknown session returns 404."""
    _, _, token, _ = await _create_test_tenant(session_factory)
    retry_resp = await client.post(
        f"/v1/sessions/{uuid.uuid4()}/retry",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert retry_resp.status_code == 404


async def test_retry_tenant_isolation(client: AsyncClient, session_factory, app):
    """A user from org A cannot retry a session from org B."""
    _, _, token_a, _ = await _create_test_tenant(session_factory)
    _, _, token_b, _ = await _create_test_tenant(session_factory)

    create_resp = await client.post(
        "/v1/sessions",
        json={"model": "gpt-4o"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    session_id = create_resp.json()["id"]
    store: SessionStore = app.state.session_store
    await store.update_session_status(UUID(session_id), "failed")

    cross_resp = await client.post(
        f"/v1/sessions/{session_id}/retry",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert cross_resp.status_code == 404


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


async def test_unauthorized_returns_401(client: AsyncClient):
    """Requests without a JWT token return 401."""
    resp = await client.get("/v1/sessions")
    assert resp.status_code == 401


async def test_tenant_isolation(client: AsyncClient, session_factory):
    """User from org A cannot see org B's sessions."""
    # Create two separate tenants
    _, _, token_a, _ = await _create_test_tenant(session_factory)
    _, _, token_b, _ = await _create_test_tenant(session_factory)

    # Create a session as tenant A
    create_resp = await client.post(
        "/v1/sessions",
        json={"model": "gpt-4o"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    session_id = create_resp.json()["id"]

    # Tenant B should NOT be able to access tenant A's session
    resp = await client.get(
        f"/v1/sessions/{session_id}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Admin -- MCP servers
# ---------------------------------------------------------------------------


async def test_admin_create_mcp_server_stdio(client: AsyncClient, session_factory):
    """POST /v1/admin/mcp-servers registers a stdio MCP server."""
    org_id, _, token, _ = await _create_test_tenant(session_factory)

    resp = await client.post(
        "/v1/admin/mcp-servers",
        json={
            "org_id": str(org_id),
            "name": "github",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "credential_refs": [
                {"name": "GITHUB_TOKEN", "env": "GITHUB_PERSONAL_ACCESS_TOKEN"}
            ],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["name"] == "github"
    assert data["transport"] == "stdio"
    assert data["command"] == "npx"
    assert data["args"] == ["-y", "@modelcontextprotocol/server-github"]
    assert data["enabled"] is True
    assert "id" in data


async def test_admin_create_mcp_server_http_requires_url(
    client: AsyncClient, session_factory
):
    """http transport without 'url' is rejected."""
    org_id, _, token, _ = await _create_test_tenant(session_factory)

    resp = await client.post(
        "/v1/admin/mcp-servers",
        json={
            "org_id": str(org_id),
            "name": "remote-mcp",
            "transport": "http",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert "url" in resp.json()["detail"].lower()


async def test_admin_create_mcp_server_stdio_requires_command(
    client: AsyncClient, session_factory
):
    """stdio transport without 'command' is rejected."""
    org_id, _, token, _ = await _create_test_tenant(session_factory)

    resp = await client.post(
        "/v1/admin/mcp-servers",
        json={
            "org_id": str(org_id),
            "name": "bad-stdio",
            "transport": "stdio",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


async def test_admin_create_mcp_server_duplicate(client: AsyncClient, session_factory):
    """Registering the same name twice at the same scope returns 409."""
    org_id, _, token, _ = await _create_test_tenant(session_factory)
    payload = {
        "org_id": str(org_id),
        "name": "dup-server",
        "transport": "stdio",
        "command": "echo",
    }

    resp1 = await client.post(
        "/v1/admin/mcp-servers",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp1.status_code == 201

    resp2 = await client.post(
        "/v1/admin/mcp-servers",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.status_code == 409


async def test_admin_list_mcp_servers(client: AsyncClient, session_factory):
    """Non-admin tenants see only their own org's MCP servers."""
    org_a, _, token_a, _ = await _create_test_tenant(
        session_factory, permissions={"admin"}
    )
    await client.post(
        "/v1/admin/mcp-servers",
        json={
            "org_id": str(org_a),
            "name": "org-a-server",
            "transport": "stdio",
            "command": "echo",
        },
        headers={"Authorization": f"Bearer {token_a}"},
    )

    _, _, token_b, _ = await _create_test_tenant(
        session_factory, permissions={"sessions:read"}
    )
    resp = await client.get(
        "/v1/admin/mcp-servers",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp.status_code == 200
    names = {s["name"] for s in resp.json()["servers"]}
    assert "org-a-server" not in names


async def test_admin_update_mcp_server(client: AsyncClient, session_factory):
    """PUT updates fields and re-validates transport/command."""
    org_id, _, token, _ = await _create_test_tenant(session_factory)
    create_resp = await client.post(
        "/v1/admin/mcp-servers",
        json={
            "org_id": str(org_id),
            "name": "updatable",
            "transport": "stdio",
            "command": "npx",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    server_id = create_resp.json()["id"]

    resp = await client.put(
        f"/v1/admin/mcp-servers/{server_id}",
        json={"enabled": False, "timeout": 60},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["enabled"] is False
    assert data["timeout"] == 60


async def test_admin_delete_mcp_server(client: AsyncClient, session_factory):
    """DELETE removes the registration."""
    org_id, _, token, _ = await _create_test_tenant(session_factory)
    create_resp = await client.post(
        "/v1/admin/mcp-servers",
        json={
            "org_id": str(org_id),
            "name": "to-delete",
            "transport": "stdio",
            "command": "echo",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    server_id = create_resp.json()["id"]

    resp = await client.delete(
        f"/v1/admin/mcp-servers/{server_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 204

    get_resp = await client.get(
        f"/v1/admin/mcp-servers/{server_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert get_resp.status_code == 404


async def test_admin_mcp_cross_org_forbidden(client: AsyncClient, session_factory):
    """Non-admin cannot register servers for a foreign org."""
    org_a, _, _, _ = await _create_test_tenant(session_factory)
    _, _, token_b, _ = await _create_test_tenant(
        session_factory, permissions={"sessions:read"}
    )

    resp = await client.post(
        "/v1/admin/mcp-servers",
        json={
            "org_id": str(org_a),
            "name": "sneaky",
            "transport": "stdio",
            "command": "echo",
        },
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Admin -- Credentials vault
# ---------------------------------------------------------------------------


async def test_admin_create_credential(client: AsyncClient, session_factory):
    """POST /v1/admin/credentials stores an encrypted credential."""
    org_id, _, token, _ = await _create_test_tenant(session_factory)

    resp = await client.post(
        "/v1/admin/credentials",
        json={
            "org_id": str(org_id),
            "name": "GITHUB_TOKEN",
            "value": "ghp_secret_value_12345",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["name"] == "GITHUB_TOKEN"
    assert data["org_id"] == str(org_id)
    # Plaintext is NEVER echoed back.
    assert "value" not in data


async def test_admin_list_credentials(client: AsyncClient, session_factory):
    """GET /v1/admin/credentials lists names only, never values."""
    org_id, _, token, _ = await _create_test_tenant(session_factory)
    for name in ("API_KEY", "DB_PASSWORD"):
        await client.post(
            "/v1/admin/credentials",
            json={"org_id": str(org_id), "name": name, "value": "secret"},
            headers={"Authorization": f"Bearer {token}"},
        )

    resp = await client.get(
        "/v1/admin/credentials",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    names = {c["name"] for c in resp.json()["credentials"]}
    assert {"API_KEY", "DB_PASSWORD"}.issubset(names)


async def test_admin_delete_credential(client: AsyncClient, session_factory):
    """DELETE removes the credential; subsequent delete returns 404."""
    org_id, _, token, _ = await _create_test_tenant(session_factory)
    await client.post(
        "/v1/admin/credentials",
        json={"org_id": str(org_id), "name": "TEMP", "value": "burn-me"},
        headers={"Authorization": f"Bearer {token}"},
    )

    resp = await client.delete(
        f"/v1/admin/credentials?org_id={org_id}&name=TEMP",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 204

    resp2 = await client.delete(
        f"/v1/admin/credentials?org_id={org_id}&name=TEMP",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.status_code == 404


async def test_admin_credentials_cross_org_forbidden(
    client: AsyncClient, session_factory
):
    """Non-admin cannot write credentials for a foreign org."""
    org_a, _, _, _ = await _create_test_tenant(session_factory)
    _, _, token_b, _ = await _create_test_tenant(
        session_factory, permissions={"sessions:read"}
    )

    resp = await client.post(
        "/v1/admin/credentials",
        json={"org_id": str(org_a), "name": "STEAL", "value": "x"},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp.status_code == 403


async def test_admin_credentials_cross_user_forbidden(
    client: AsyncClient, session_factory
):
    """Non-admin cannot touch another user's credentials in the same org."""
    org_id, victim_user_id, _, _ = await _create_test_tenant(
        session_factory, permissions={"sessions:read"}
    )
    attacker_user_id = uuid.uuid4()
    await create_user(
        session_factory, org_id,
        user_id=attacker_user_id,
        email=f"attacker-{attacker_user_id}@test.com",
    )
    attacker_token = create_access_token(
        org_id, attacker_user_id, {"sessions:read"},
    )

    create_resp = await client.post(
        "/v1/admin/credentials",
        json={
            "org_id": str(org_id),
            "user_id": str(victim_user_id),
            "name": "VICTIM_TOKEN",
            "value": "overwrite",
        },
        headers={"Authorization": f"Bearer {attacker_token}"},
    )
    assert create_resp.status_code == 403

    list_resp = await client.get(
        f"/v1/admin/credentials?user_id={victim_user_id}",
        headers={"Authorization": f"Bearer {attacker_token}"},
    )
    assert list_resp.status_code == 403

    delete_resp = await client.delete(
        f"/v1/admin/credentials?org_id={org_id}"
        f"&user_id={victim_user_id}&name=VICTIM_TOKEN",
        headers={"Authorization": f"Bearer {attacker_token}"},
    )
    assert delete_resp.status_code == 403


async def test_admin_mcp_cross_user_forbidden(
    client: AsyncClient, session_factory
):
    """Non-admin cannot touch another user's MCP server in the same org."""
    org_id, victim_user_id, _, _ = await _create_test_tenant(session_factory)
    attacker_user_id = uuid.uuid4()
    await create_user(
        session_factory, org_id,
        user_id=attacker_user_id,
        email=f"attacker-{attacker_user_id}@test.com",
    )
    attacker_token = create_access_token(
        org_id, attacker_user_id, {"sessions:read"},
    )

    resp = await client.post(
        "/v1/admin/mcp-servers",
        json={
            "org_id": str(org_id),
            "user_id": str(victim_user_id),
            "name": "malicious",
            "transport": "stdio",
            "command": "/bin/attacker",
        },
        headers={"Authorization": f"Bearer {attacker_token}"},
    )
    assert resp.status_code == 403


async def test_admin_credential_roundtrip_via_vault(
    client: AsyncClient, session_factory, app
):
    """Ciphertext stored via the API decrypts to the original plaintext."""
    org_id, _, token, _ = await _create_test_tenant(session_factory)

    resp = await client.post(
        "/v1/admin/credentials",
        json={
            "org_id": str(org_id),
            "name": "ROUNDTRIP",
            "value": "s3cr3t-v4lu3",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201

    plaintext = await app.state.credential_vault.retrieve(org_id, "ROUNDTRIP")
    assert plaintext == "s3cr3t-v4lu3"


async def test_admin_credential_upsert_returns_200_on_update(
    client: AsyncClient, session_factory
):
    """First POST returns 201; subsequent POST to same name returns 200."""
    org_id, _, token, _ = await _create_test_tenant(session_factory)

    resp1 = await client.post(
        "/v1/admin/credentials",
        json={"org_id": str(org_id), "name": "ROTATE", "value": "v1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp1.status_code == 201

    resp2 = await client.post(
        "/v1/admin/credentials",
        json={"org_id": str(org_id), "name": "ROTATE", "value": "v2"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.status_code == 200


async def test_admin_credentials_503_when_vault_disabled(
    client: AsyncClient, session_factory, app
):
    """Endpoints return 503 when the credential vault isn't provisioned."""
    _, _, token, _ = await _create_test_tenant(session_factory)
    original = app.state.credential_vault
    app.state.credential_vault = None
    try:
        resp = await client.get(
            "/v1/admin/credentials",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 503
    finally:
        app.state.credential_vault = original


async def test_admin_mcp_update_http_requires_url(
    client: AsyncClient, session_factory
):
    """PUT re-validates transport/command/url pairing."""
    org_id, _, token, _ = await _create_test_tenant(session_factory)

    # Create a stdio server first.
    create_resp = await client.post(
        "/v1/admin/mcp-servers",
        json={
            "org_id": str(org_id),
            "name": "to-flip",
            "transport": "stdio",
            "command": "echo",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    server_id = create_resp.json()["id"]

    resp = await client.put(
        f"/v1/admin/mcp-servers/{server_id}",
        json={"transport": "http"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert "url" in resp.json()["detail"].lower()


async def test_admin_mcp_user_id_must_belong_to_org(
    client: AsyncClient, session_factory
):
    """Creating a user-scoped server for a user in a different org returns 404."""
    _, _, token, _ = await _create_test_tenant(session_factory)
    foreign_org_id = await create_org(session_factory)
    foreign_user_id = await create_user(session_factory, foreign_org_id)

    # Admin permission lets the caller target the foreign org directly —
    # this call should succeed, proving the FK check only bites when
    # user_id is presented outside its true org.
    resp = await client.post(
        "/v1/admin/mcp-servers",
        json={
            "org_id": str(foreign_org_id),
            "user_id": str(foreign_user_id),
            "name": "foreign-scope",
            "transport": "stdio",
            "command": "echo",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201

    unrelated_user_id = await create_user(session_factory, await create_org(session_factory))
    resp = await client.post(
        "/v1/admin/mcp-servers",
        json={
            "org_id": str(foreign_org_id),
            "user_id": str(unrelated_user_id),
            "name": "wrong-org-user",
            "transport": "stdio",
            "command": "echo",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_admin_mcp_duplicate_user_scope_409(
    client: AsyncClient, session_factory
):
    """Registering the same name twice at user scope returns 409."""
    org_id, user_id, token, _ = await _create_test_tenant(session_factory)
    payload = {
        "org_id": str(org_id),
        "user_id": str(user_id),
        "name": "dup-user",
        "transport": "stdio",
        "command": "echo",
    }
    resp1 = await client.post(
        "/v1/admin/mcp-servers", json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp1.status_code == 201

    resp2 = await client.post(
        "/v1/admin/mcp-servers", json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.status_code == 409


async def test_admin_mcp_list_cross_org_for_admin(
    client: AsyncClient, session_factory
):
    """Platform admin can filter list by org_id."""
    org_a, _, _, _ = await _create_test_tenant(session_factory)
    org_b, _, _, _ = await _create_test_tenant(session_factory)
    _, _, admin_token, _ = await _create_test_tenant(
        session_factory, permissions={"admin"}
    )

    for oid, name in [(org_a, "a-server"), (org_b, "b-server")]:
        await client.post(
            "/v1/admin/mcp-servers",
            json={
                "org_id": str(oid),
                "name": name,
                "transport": "stdio",
                "command": "echo",
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    resp = await client.get(
        f"/v1/admin/mcp-servers?org_id={org_a}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    names = {s["name"] for s in resp.json()["servers"]}
    assert "a-server" in names
    assert "b-server" not in names
