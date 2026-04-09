"""Integration tests for the FastAPI application against real PostgreSQL + Redis."""

from __future__ import annotations

import os
import uuid
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from surogates.session.store import SessionStore
from surogates.tenant.auth.jwt import create_access_token

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
