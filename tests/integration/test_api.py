"""Integration tests for the FastAPI application against real PostgreSQL + Redis."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from surogates.db.models import Event, InboxItem
from surogates.session.events import EventType
from surogates.session.store import SessionStore
from surogates.storage.tenant import session_workspace_key
from surogates.tenant.auth.jwt import create_access_token
from surogates.tenant.credentials import CredentialVault

from .conftest import create_org, create_user, issue_service_account_token

pytestmark = pytest.mark.asyncio(loop_scope="session")

TEST_AGENT_ID = "default"


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
    application.state.settings.storage.bucket = f"test-agent-{uuid.uuid4()}"

    from surogates.storage.backend import create_backend
    application.state.storage = create_backend(application.state.settings)

    # Vault for /v1/admin/credentials and MCP credential refs.
    application.state.credential_vault = CredentialVault(
        session_factory, Fernet.generate_key()
    )

    # Routes resolve the target agent via ``agent_runtime_context_dep``, which
    # reaches for ``runtime_config_cache`` + an ``agent_id`` the bare-token test
    # requests don't supply.  Override the dependency with a fixed shared-runtime
    # context so the tests exercise the route handlers rather than agent
    # resolution wiring.
    from surogates.runtime import (
        agent_runtime_context_dep,
        build_agent_runtime_context,
    )

    def _fixed_runtime_context():
        return build_agent_runtime_context({
            "agent_id": TEST_AGENT_ID,
            "org_id": "00000000-0000-0000-0000-000000000000",
            "project_id": "test-project",
            "enabled": True,
            "version": 1,
            "storage_key_prefix": "",
        })

    application.dependency_overrides[agent_runtime_context_dep] = (
        _fixed_runtime_context
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


async def _create_scheduled_run_session(app, session_factory, org_id, user_id):
    store = SessionStore(session_factory)
    return await store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id=TEST_AGENT_ID,
        channel="scheduled",
        # No model: the worker stamps the resolved bundle model at wake.
        config={
            "storage_bucket": app.state.settings.storage.bucket,
            "scheduled_session_id": str(uuid.uuid4()),
        },
    )


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
    # The organisation by its name, as Surogate Desktop's Settings shows it.
    assert data["org_name"] == f"test-org-{org_id}"


async def test_auth_me_update_names_the_organisation(client: AsyncClient, session_factory):
    """PATCH /v1/auth/me answers as GET does, the organisation's name included."""
    org_id, _, token, _ = await _create_test_tenant(session_factory)

    resp = await client.patch(
        "/v1/auth/me",
        json={"display_name": "Flavius"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["display_name"] == "Flavius"
    assert resp.json()["org_name"] == f"test-org-{org_id}"


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


async def test_define_outcome_event_requires_rubric(
    client: AsyncClient,
    session_factory,
):
    _, _, token, _ = await _create_test_tenant(session_factory)
    create_resp = await client.post(
        "/v1/sessions",
        json={"model": "gpt-4o"},
        headers={"Authorization": f"Bearer {token}"},
    )
    session_id = create_resp.json()["id"]

    response = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "events": [
                {
                    "type": "user.define_outcome",
                    "description": "Fix all tests",
                    "max_iterations": 3,
                }
            ]
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert "rubric" in response.text


async def test_define_outcome_event_persists_state_and_enqueues(
    client: AsyncClient,
    session_factory,
    app,
):
    _, _, token, _ = await _create_test_tenant(session_factory)
    create_resp = await client.post(
        "/v1/sessions",
        json={"model": "gpt-4o"},
        headers={"Authorization": f"Bearer {token}"},
    )
    session_id = UUID(create_resp.json()["id"])

    response = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "events": [
                {
                    "type": "user.define_outcome",
                    "description": "Fix all tests",
                    "rubric": {"type": "text", "content": "- pytest passes"},
                    "max_iterations": 5,
                }
            ]
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["events"][0]["type"] == "user.define_outcome"
    assert body["events"][0]["outcome_id"].startswith("outc_")

    session = await app.state.session_store.get_session(session_id)
    assert session.config["outcome"]["description"] == "Fix all tests"
    assert session.config["outcome"]["rubric"] == "- pytest passes"
    assert session.config["outcome"]["max_iterations"] == 5


async def test_send_message_revives_completed_session(
    client: AsyncClient,
    session_factory,
):
    """A completed objective can receive a follow-up objective."""
    _, _, token, _ = await _create_test_tenant(session_factory)

    create_resp = await client.post(
        "/v1/sessions",
        json={"model": "gpt-4o"},
        headers={"Authorization": f"Bearer {token}"},
    )
    session_id = UUID(create_resp.json()["id"])

    store = SessionStore(session_factory)
    await store.update_session_status(session_id, "completed")

    resp = await client.post(
        f"/v1/sessions/{session_id}/messages",
        json={"content": "New objective"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 202, resp.text
    data = resp.json()
    assert data["event_id"] > 0
    assert data["status"] == "processing"

    events = await store.get_events(session_id)
    assert any(e.type == "session.resume" for e in events)


async def test_create_api_session_with_service_account(
    client: AsyncClient, session_factory
):
    """POST /v1/api/sessions creates an API-channel service-account session."""
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(
        session_factory, org_id, name="ops-chat"
    )

    resp = await client.post(
        "/v1/api/sessions",
        json={"system": "Keep replies concise.", "config": {"source": "ops"}},
        headers={"Authorization": f"Bearer {issued.token}"},
    )

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "active"
    assert data["channel"] == "api"
    assert "id" in data

    store = SessionStore(session_factory)
    session = await store.get_session(UUID(data["id"]))
    assert session.service_account_id == issued.id
    assert session.user_id is None
    assert session.config["system"] == "Keep replies concise."
    assert session.config["source"] == "ops"


async def test_api_session_message_with_service_account(
    client: AsyncClient, session_factory
):
    """POST /v1/api/sessions/{id}/messages accepts service-account tokens."""
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(
        session_factory, org_id, name="ops-chat"
    )
    headers = {"Authorization": f"Bearer {issued.token}"}
    create_resp = await client.post(
        "/v1/api/sessions",
        json={"config": {"source": "ops"}},
        headers=headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    session_id = create_resp.json()["id"]

    resp = await client.post(
        f"/v1/api/sessions/{session_id}/messages",
        json={"content": "Hello from Ops"},
        headers=headers,
    )

    assert resp.status_code == 202, resp.text
    data = resp.json()
    assert data["event_id"] > 0
    assert data["status"] == "processing"


async def test_api_session_create_rejects_user_jwt(
    client: AsyncClient, session_factory
):
    """The /v1/api/sessions creation route is service-account only."""
    _, _, token, _ = await _create_test_tenant(session_factory)

    resp = await client.post(
        "/v1/api/sessions",
        json={"config": {"source": "ops"}},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 403
    assert "service-account" in resp.json()["detail"].lower()


async def test_patch_api_session_updates_title(
    client: AsyncClient, session_factory
):
    """PATCH /v1/api/sessions/{id} sets sessions.title for a service-account."""
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(
        session_factory, org_id, name="ops-chat"
    )
    headers = {"Authorization": f"Bearer {issued.token}"}
    create_resp = await client.post(
        "/v1/api/sessions",
        json={"config": {"source": "ops"}},
        headers=headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    session_id = create_resp.json()["id"]

    resp = await client.patch(
        f"/v1/api/sessions/{session_id}",
        json={"title": "Debugging model drift"},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] == session_id
    assert data["title"] == "Debugging model drift"

    store = SessionStore(session_factory)
    persisted = await store.get_session(UUID(session_id))
    assert persisted.title == "Debugging model drift"


async def test_patch_api_session_overwrites_existing_title(
    client: AsyncClient, session_factory
):
    """Repeated PATCHes overwrite a prior (auto-generated) title."""
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(
        session_factory, org_id, name="ops-chat"
    )
    headers = {"Authorization": f"Bearer {issued.token}"}
    create_resp = await client.post(
        "/v1/api/sessions",
        json={"config": {"source": "ops"}},
        headers=headers,
    )
    session_id = create_resp.json()["id"]

    store = SessionStore(session_factory)
    # Simulate the harness-generated title that ``update_session_title_if_empty``
    # would have written.
    assert await store.update_session_title_if_empty(
        UUID(session_id), "Auto Title"
    ) is True

    resp = await client.patch(
        f"/v1/api/sessions/{session_id}",
        json={"title": "User Renamed"},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "User Renamed"

    # And a second user rename must overwrite again.
    resp2 = await client.patch(
        f"/v1/api/sessions/{session_id}",
        json={"title": "Renamed Again"},
        headers=headers,
    )
    assert resp2.status_code == 200
    assert resp2.json()["title"] == "Renamed Again"

    persisted = await store.get_session(UUID(session_id))
    assert persisted.title == "Renamed Again"


async def test_patch_api_session_rejects_blank_title(
    client: AsyncClient, session_factory
):
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(
        session_factory, org_id, name="ops-chat"
    )
    headers = {"Authorization": f"Bearer {issued.token}"}
    create_resp = await client.post(
        "/v1/api/sessions",
        json={"config": {"source": "ops"}},
        headers=headers,
    )
    session_id = create_resp.json()["id"]

    resp = await client.patch(
        f"/v1/api/sessions/{session_id}",
        json={"title": "   "},
        headers=headers,
    )

    assert resp.status_code == 422, resp.text


async def test_patch_api_session_rejects_long_title(
    client: AsyncClient, session_factory
):
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(
        session_factory, org_id, name="ops-chat"
    )
    headers = {"Authorization": f"Bearer {issued.token}"}
    create_resp = await client.post(
        "/v1/api/sessions",
        json={"config": {"source": "ops"}},
        headers=headers,
    )
    session_id = create_resp.json()["id"]

    resp = await client.patch(
        f"/v1/api/sessions/{session_id}",
        json={"title": "x" * 257},
        headers=headers,
    )

    assert resp.status_code == 422, resp.text


async def test_patch_api_session_rejects_user_jwt(
    client: AsyncClient, session_factory
):
    """The /v1/api/sessions PATCH alias is service-account only."""
    org_id, _, token, _ = await _create_test_tenant(session_factory)
    issued = await issue_service_account_token(
        session_factory, org_id, name="ops-chat"
    )
    create_resp = await client.post(
        "/v1/api/sessions",
        json={"config": {"source": "ops"}},
        headers={"Authorization": f"Bearer {issued.token}"},
    )
    session_id = create_resp.json()["id"]

    resp = await client.patch(
        f"/v1/api/sessions/{session_id}",
        json={"title": "User rename"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 403
    assert "service-account" in resp.json()["detail"].lower()


async def test_patch_session_user_route_renames(
    client: AsyncClient, session_factory
):
    """PATCH /v1/sessions/{id} (web alias) renames for the owning user."""
    _, _, token, _ = await _create_test_tenant(session_factory)
    create_resp = await client.post(
        "/v1/sessions",
        json={"model": "gpt-4o"},
        headers={"Authorization": f"Bearer {token}"},
    )
    session_id = create_resp.json()["id"]

    resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"title": "My session"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "My session"


async def test_api_session_workspace_file_with_service_account(
    client: AsyncClient, app, session_factory
):
    """Service-account sessions can browse workspace files through /v1/api."""
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(
        session_factory, org_id, name="ops-chat"
    )
    headers = {"Authorization": f"Bearer {issued.token}"}
    create_resp = await client.post(
        "/v1/api/sessions",
        json={"config": {"source": "ops"}},
        headers=headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    session_id = create_resp.json()["id"]

    store = SessionStore(session_factory)
    session = await store.get_session(UUID(session_id))
    bucket = session.config["storage_bucket"]
    await app.state.storage.write(
        bucket,
        session_workspace_key(session_id, "src/main.py"),
        b"print('hello')\n",
    )

    tree_resp = await client.get(
        f"/v1/api/sessions/{session_id}/workspace/tree",
        headers=headers,
    )
    assert tree_resp.status_code == 200, tree_resp.text
    assert tree_resp.json()["entries"] == [
        {
            "name": "src",
            "path": "src",
            "kind": "dir",
            "size": None,
            "children": [
                {
                    "name": "main.py",
                    "path": "src/main.py",
                    "kind": "file",
                    "size": None,
                    "children": None,
                }
            ],
        }
    ]

    file_resp = await client.get(
        f"/v1/api/sessions/{session_id}/workspace/file",
        params={"path": "src/main.py"},
        headers=headers,
    )
    assert file_resp.status_code == 200, file_resp.text
    assert file_resp.json()["content"] == "print('hello')\n"


async def test_workspace_routes_resolve_root_for_child_session(
    client: AsyncClient, app, session_factory
):
    """A child session's workspace viewer must list the parent's files.

    Regression: workspace.py used to key storage on the URL session_id,
    so a delegation child or loop iteration would see an empty workspace
    even though the harness was writing to the parent's prefix.  Files
    written under ``sessions/{parent_id}/`` must surface when the child's
    ``/workspace/tree`` is queried, and reads/uploads/downloads/deletes
    must go through the parent's prefix too.
    """
    org_id, user_id, token, _ = await _create_test_tenant(session_factory)
    headers = {"Authorization": f"Bearer {token}"}

    # Parent session goes through the normal creation path so it gets
    # workspace fields seeded.
    parent_resp = await client.post(
        "/v1/sessions",
        json={"model": "gpt-4o"},
        headers=headers,
    )
    assert parent_resp.status_code == 201, parent_resp.text
    parent_id = UUID(parent_resp.json()["id"])

    store = SessionStore(session_factory)
    parent = await store.get_session(parent_id)
    bucket = parent.config["storage_bucket"]

    # Files actually live under the parent's prefix.
    await app.state.storage.write(
        bucket,
        session_workspace_key(parent_id, "notes/plan.md"),
        b"# plan\n",
    )

    # Create a delegation-style child sharing the parent's workspace,
    # mirroring how create_child_session stamps the config.
    from surogates.session.provisioning import create_child_session
    child = await create_child_session(
        store=store,
        parent=parent,
        channel="delegation",
    )

    # Child's tree must show the parent's file.
    tree_resp = await client.get(
        f"/v1/sessions/{child.id}/workspace/tree",
        headers=headers,
    )
    assert tree_resp.status_code == 200, tree_resp.text
    tree = tree_resp.json()
    notes_dir = next(e for e in tree["entries"] if e["name"] == "notes")
    file_names = [c["name"] for c in notes_dir["children"]]
    assert "plan.md" in file_names

    # Read via the child route surfaces the parent's content.
    file_resp = await client.get(
        f"/v1/sessions/{child.id}/workspace/file",
        params={"path": "notes/plan.md"},
        headers=headers,
    )
    assert file_resp.status_code == 200, file_resp.text
    assert file_resp.json()["content"] == "# plan\n"

    # Upload via the child route writes into the parent's prefix.
    upload_resp = await client.post(
        f"/v1/sessions/{child.id}/workspace/upload",
        files={"file": ("from_child.txt", b"hello from child")},
        params={"path": "shared"},
        headers=headers,
    )
    assert upload_resp.status_code == 201, upload_resp.text
    assert await app.state.storage.exists(
        bucket,
        session_workspace_key(parent_id, "shared/from_child.txt"),
    )
    # And the same file does NOT exist under the child's own prefix.
    assert not await app.state.storage.exists(
        bucket,
        session_workspace_key(child.id, "shared/from_child.txt"),
    )

    # Download routes resolve through the parent too.
    dl_resp = await client.get(
        f"/v1/sessions/{child.id}/workspace/download",
        params={"path": "shared/from_child.txt", "token": token},
    )
    assert dl_resp.status_code == 200
    assert dl_resp.content == b"hello from child"

    # Delete via the child route removes the file from the parent's prefix.
    del_resp = await client.request(
        "DELETE",
        f"/v1/sessions/{child.id}/workspace/file",
        params={"path": "shared/from_child.txt"},
        headers=headers,
    )
    assert del_resp.status_code == 200, del_resp.text
    assert not await app.state.storage.exists(
        bucket,
        session_workspace_key(parent_id, "shared/from_child.txt"),
    )


async def test_workspace_legacy_child_without_root_uses_own_prefix(
    client: AsyncClient, app, session_factory
):
    """Pre-deploy children without sandbox_root_session_id keep their own prefix.

    Legacy scheduled-runner runs were created with ``create_agent_session``
    and own a per-session storage prefix.  The viewer must continue to
    surface their own files; falling through to ``parent_id`` would
    silently switch them to an unrelated prefix.
    """
    org_id, user_id, token, _ = await _create_test_tenant(session_factory)
    headers = {"Authorization": f"Bearer {token}"}

    parent_resp = await client.post(
        "/v1/sessions",
        json={"model": "gpt-4o"},
        headers=headers,
    )
    parent_id = UUID(parent_resp.json()["id"])

    store = SessionStore(session_factory)
    parent = await store.get_session(parent_id)
    bucket = parent.config["storage_bucket"]

    # Simulate a legacy child: has parent_id but no sandbox_root_session_id.
    # Files live under sessions/{child_id}/, as the old scheduled runner
    # would have allocated via create_agent_session.
    legacy_child_id = uuid.uuid4()
    await store.create_session(
        session_id=legacy_child_id,
        user_id=user_id,
        org_id=org_id,
        agent_id=parent.agent_id,
        channel="scheduled",
        model=parent.model,
        config={
            "storage_bucket": bucket,
            "workspace_path": f"/workspace/{bucket}/sessions/{legacy_child_id}",
            "supports_vision": False,
        },
        parent_id=parent_id,
    )
    await app.state.storage.write(
        bucket,
        session_workspace_key(legacy_child_id, "legacy.txt"),
        b"legacy content",
    )

    tree_resp = await client.get(
        f"/v1/sessions/{legacy_child_id}/workspace/tree",
        headers=headers,
    )
    assert tree_resp.status_code == 200, tree_resp.text
    entries = tree_resp.json()["entries"]
    assert any(e["name"] == "legacy.txt" for e in entries), entries

    file_resp = await client.get(
        f"/v1/sessions/{legacy_child_id}/workspace/file",
        params={"path": "legacy.txt"},
        headers=headers,
    )
    assert file_resp.status_code == 200, file_resp.text
    assert file_resp.json()["content"] == "legacy content"


async def test_session_workspace_download_accepts_query_token(
    client: AsyncClient, app, session_factory
):
    """Workspace download supports browser anchor links carrying ?token=."""
    _, _, token, _ = await _create_test_tenant(session_factory)
    create_resp = await client.post(
        "/v1/sessions",
        json={"model": "gpt-4o"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert create_resp.status_code == 201, create_resp.text
    session_id = create_resp.json()["id"]

    store = SessionStore(session_factory)
    session = await store.get_session(UUID(session_id))
    bucket = session.config["storage_bucket"]
    await app.state.storage.write(
        bucket,
        session_workspace_key(session_id, "browser-screenshots/shot.png"),
        b"png bytes",
    )

    download_resp = await client.get(
        f"/v1/sessions/{session_id}/workspace/download",
        params={"path": "browser-screenshots/shot.png", "token": token},
    )

    assert download_resp.status_code == 200, download_resp.text
    assert download_resp.content == b"png bytes"
    assert download_resp.headers["content-disposition"] == (
        'attachment; filename="shot.png"'
    )


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


async def test_retry_failed_session(client: AsyncClient, session_factory, app):
    """POST /retry on a failed session re-enqueues it and flips to active."""
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

    # Drain any pre-existing entries from the shared work queue so we can
    # assert the retry enqueue.  Every enqueue lands on a single shared
    # key; members encode the (org_id, agent_id, session_id) tuple.
    from surogates.config import SHARED_WORK_QUEUE_KEY, parse_queue_member
    await app.state.redis.delete(SHARED_WORK_QUEUE_KEY)

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

    # Shared work queue contains a member for the session id.
    queued = await app.state.redis.zrange(SHARED_WORK_QUEUE_KEY, 0, -1)
    queued_session_ids = [
        parse_queue_member(
            member.decode() if isinstance(member, bytes) else member
        )[2]
        for member in queued
    ]
    assert session_id in queued_session_ids, (
        "expected session to be re-enqueued"
    )


async def test_retry_paused_session(client: AsyncClient, session_factory, app):
    """Retry also works for paused sessions (same semantics as /resume)."""
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


async def test_admin_credentials_service_account_scope_visible_and_deletable(
    client, session_factory
):
    """Service-account-scoped credentials are not reported as org-scoped."""
    from sqlalchemy import text

    _, _, token, _ = await _create_test_tenant(
        session_factory, permissions={"admin"}
    )
    org_id = await create_org(session_factory)
    sa_id = uuid.uuid4()
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO service_accounts "
                "(id, org_id, name, token_hash, token_prefix) "
                "VALUES (:id, :org_id, :name, :hash, :prefix)"
            ),
            {
                "id": sa_id,
                "org_id": org_id,
                "name": f"agent:{sa_id}",
                "hash": f"h:{sa_id}",
                "prefix": str(sa_id)[:8],
            },
        )
        await db.commit()

    create = await client.post(
        "/v1/admin/credentials",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "org_id": str(org_id),
            "service_account_id": str(sa_id),
            "name": "agent-byo-key",
            "value": "sk-agent",
        },
    )
    assert create.status_code == 201
    assert create.json()["service_account_id"] == str(sa_id)
    assert create.json()["user_id"] is None

    listed = await client.get(
        f"/v1/admin/credentials?service_account_id={sa_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert listed.status_code == 200
    assert listed.json()["credentials"] == [
        {
            "org_id": str(org_id),
            "user_id": None,
            "service_account_id": str(sa_id),
            "name": "agent-byo-key",
        }
    ]

    deleted = await client.delete(
        f"/v1/admin/credentials?org_id={org_id}"
        f"&service_account_id={sa_id}&name=agent-byo-key",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert deleted.status_code == 204


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


# ---------------------------------------------------------------------------
# Admin -- every admin route needs the admin permission
# ---------------------------------------------------------------------------

#: What the admin MCP API shows in place of every ``env`` value.
REDACTED_ENV_VALUE = "__redacted__"

_NON_ADMIN_PRINCIPALS = [
    "end-user",
    "channel-session",
    "service-account-session",
    "service-account",
]


async def _non_admin_token(session_factory, org_id: UUID, principal: str) -> str:
    """A token of the given kind, issued inside *org_id* and holding no ``admin``."""
    from surogates.api.routes.auth import USER_PERMISSIONS
    from surogates.tenant.auth.jwt import (
        create_channel_session_token,
        create_service_account_session_token,
    )

    if principal == "end-user":
        user_id = await create_user(session_factory, org_id)
        return create_access_token(org_id, user_id, USER_PERMISSIONS)
    if principal == "service-account":
        # A bare ``surg_sk_`` key: the auth middleware refuses it on every
        # route outside ``/v1/api`` before any route dependency runs.
        return (await issue_service_account_token(session_factory, org_id)).token
    if principal == "channel-session":
        session = await SessionStore(session_factory).create_session(
            user_id=None, org_id=org_id, agent_id=TEST_AGENT_ID, channel="website",
        )
        return create_channel_session_token(
            org_id, TEST_AGENT_ID, session.id, "website",
        )
    assert principal == "service-account-session"
    service_account = await issue_service_account_token(session_factory, org_id)
    return create_service_account_session_token(
        org_id, service_account.id, uuid.uuid4(),
    )


async def _stored_mcp_env(session_factory, server_id: str) -> dict[str, str]:
    from surogates.db.models import McpServer

    async with session_factory() as db:
        row = await db.get(McpServer, UUID(server_id))
        return dict(row.env)


async def test_admin_mcp_read_masks_env_values(client: AsyncClient, session_factory):
    """Create, get and list show an MCP server's env keys with every value masked."""
    org_id, _, token, _ = await _create_test_tenant(session_factory)
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/v1/admin/mcp-servers",
        json={
            "org_id": str(org_id),
            "name": "masked",
            "transport": "stdio",
            "command": "echo",
            "env": {"API_KEY": "sk-live-1234", "REGION": "eu-west-1"},
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    masked = {"API_KEY": REDACTED_ENV_VALUE, "REGION": REDACTED_ENV_VALUE}
    assert created.json()["env"] == masked
    server_id = created.json()["id"]

    got = await client.get(f"/v1/admin/mcp-servers/{server_id}", headers=headers)
    assert got.status_code == 200
    assert got.json()["env"] == masked

    listed = await client.get(
        f"/v1/admin/mcp-servers?org_id={org_id}", headers=headers,
    )
    assert listed.status_code == 200
    assert [s["env"] for s in listed.json()["servers"]] == [masked]
    assert "sk-live-1234" not in listed.text and "eu-west-1" not in listed.text

    # Masking is a view: the proxy still reads the real values.
    assert await _stored_mcp_env(session_factory, server_id) == {
        "API_KEY": "sk-live-1234", "REGION": "eu-west-1",
    }


async def test_admin_mcp_put_keeps_stored_env_value_for_placeholder(
    client: AsyncClient, session_factory
):
    """A PUT that echoes the placeholder keeps the stored value; a bare one is dropped."""
    org_id, _, token, _ = await _create_test_tenant(session_factory)
    headers = {"Authorization": f"Bearer {token}"}
    created = await client.post(
        "/v1/admin/mcp-servers",
        json={
            "org_id": str(org_id),
            "name": "echoed",
            "transport": "stdio",
            "command": "echo",
            "env": {"API_KEY": "sk-live-1234", "OLD": "gone"},
        },
        headers=headers,
    )
    server_id = created.json()["id"]

    resp = await client.put(
        f"/v1/admin/mcp-servers/{server_id}",
        json={
            "env": {
                "API_KEY": REDACTED_ENV_VALUE,
                "GHOST": REDACTED_ENV_VALUE,
                "NEW": "n-1",
            },
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["env"] == {
        "API_KEY": REDACTED_ENV_VALUE, "NEW": REDACTED_ENV_VALUE,
    }
    assert await _stored_mcp_env(session_factory, server_id) == {
        "API_KEY": "sk-live-1234", "NEW": "n-1",
    }

    # A PUT that leaves env out keeps it whole.
    resp = await client.put(
        f"/v1/admin/mcp-servers/{server_id}", json={"timeout": 30}, headers=headers,
    )
    assert resp.status_code == 200
    assert await _stored_mcp_env(session_factory, server_id) == {
        "API_KEY": "sk-live-1234", "NEW": "n-1",
    }


async def test_admin_mcp_response_cannot_carry_an_env_value():
    """The response model refuses an env value that is not the placeholder."""
    from pydantic import ValidationError

    from surogates.api.routes.admin_mcp import McpServerResponse

    fields = dict(
        id=uuid.uuid4(), org_id=uuid.uuid4(), user_id=None, name="n",
        transport="stdio", command="echo", args=[], url=None,
        credential_refs=[], timeout=120, enabled=True,
    )
    McpServerResponse(env={"K": REDACTED_ENV_VALUE}, **fields)
    with pytest.raises(ValidationError):
        McpServerResponse(env={"K": "sk-live-1234"}, **fields)


@pytest.mark.parametrize("principal", _NON_ADMIN_PRINCIPALS)
async def test_admin_mcp_routes_refuse_non_admin_principals(
    client: AsyncClient, session_factory, principal: str
):
    """Every verb of the admin MCP router is 403 without the admin permission."""
    org_id, _, admin_token, _ = await _create_test_tenant(session_factory)
    admin = {"Authorization": f"Bearer {admin_token}"}
    seeded = await client.post(
        "/v1/admin/mcp-servers",
        json={
            "org_id": str(org_id),
            "name": "org-wide",
            "transport": "stdio",
            "command": "echo",
            "env": {"API_KEY": "sk-live-1234"},
        },
        headers=admin,
    )
    server_id = seeded.json()["id"]
    caller = {
        "Authorization": "Bearer "
        + await _non_admin_token(session_factory, org_id, principal),
    }

    refused = [
        await client.post(
            "/v1/admin/mcp-servers",
            json={
                "org_id": str(org_id), "name": "new",
                "transport": "stdio", "command": "echo",
            },
            headers=caller,
        ),
        await client.get("/v1/admin/mcp-servers", headers=caller),
        await client.get(f"/v1/admin/mcp-servers/{server_id}", headers=caller),
        await client.put(
            f"/v1/admin/mcp-servers/{server_id}",
            json={"enabled": False}, headers=caller,
        ),
        await client.delete(f"/v1/admin/mcp-servers/{server_id}", headers=caller),
    ]
    assert [r.status_code for r in refused] == [403] * 5, [r.text for r in refused]

    # Nothing the refused calls asked for happened.
    after = await client.get(f"/v1/admin/mcp-servers/{server_id}", headers=admin)
    assert after.status_code == 200
    assert after.json()["enabled"] is True
    listed = await client.get(f"/v1/admin/mcp-servers?org_id={org_id}", headers=admin)
    assert [s["name"] for s in listed.json()["servers"]] == ["org-wide"]


@pytest.mark.parametrize("principal", _NON_ADMIN_PRINCIPALS)
async def test_admin_credentials_routes_refuse_non_admin_principals(
    client: AsyncClient, session_factory, app, principal: str
):
    """Every verb of the admin credentials router is 403 without the admin permission."""
    org_id, _, admin_token, _ = await _create_test_tenant(session_factory)
    admin = {"Authorization": f"Bearer {admin_token}"}
    seeded = await client.post(
        "/v1/admin/credentials",
        json={"org_id": str(org_id), "name": "ORG_KEY", "value": "keep-me"},
        headers=admin,
    )
    assert seeded.status_code == 201
    caller = {
        "Authorization": "Bearer "
        + await _non_admin_token(session_factory, org_id, principal),
    }

    refused = [
        await client.post(
            "/v1/admin/credentials",
            json={"org_id": str(org_id), "name": "NEW_KEY", "value": "x"},
            headers=caller,
        ),
        await client.post(
            "/v1/admin/credentials",
            json={"org_id": str(org_id), "name": "ORG_KEY", "value": "overwritten"},
            headers=caller,
        ),
        await client.get(f"/v1/admin/credentials?org_id={org_id}", headers=caller),
        await client.delete(
            f"/v1/admin/credentials?org_id={org_id}&name=ORG_KEY", headers=caller,
        ),
    ]
    assert [r.status_code for r in refused] == [403] * 4, [r.text for r in refused]

    vault = app.state.credential_vault
    assert await vault.retrieve(org_id, "ORG_KEY") == "keep-me"
    assert await vault.retrieve(org_id, "NEW_KEY") is None


async def _org_user_ids(client: AsyncClient, org_id: UUID, headers: dict) -> set[str]:
    resp = await client.get(
        f"/v1/admin/orgs/{org_id}/users?limit=200", headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return {u["id"] for u in resp.json()["users"]}


async def _org_count(client: AsyncClient, headers: dict) -> int:
    resp = await client.get("/v1/admin/orgs?limit=1", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["total"]


async def test_admin_get_org_and_list_users(client: AsyncClient, session_factory):
    """An admin reads an org and lists its users."""
    org_id, user_id, token, _ = await _create_test_tenant(session_factory)
    headers = {"Authorization": f"Bearer {token}"}

    got = await client.get(f"/v1/admin/orgs/{org_id}", headers=headers)
    assert got.status_code == 200, got.text
    assert got.json()["id"] == str(org_id)

    created = await client.post(
        f"/v1/admin/orgs/{org_id}/users",
        json={
            "email": "listed@test.com",
            "display_name": "Listed",
            "password": "secret123",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    assert await _org_user_ids(client, org_id, headers) == {
        str(user_id), created.json()["id"],
    }


@pytest.mark.parametrize("principal", _NON_ADMIN_PRINCIPALS)
async def test_admin_org_routes_refuse_non_admin_principals(
    client: AsyncClient, session_factory, principal: str
):
    """Every verb of the admin org router is 403 without the admin permission."""
    org_id, _, admin_token, _ = await _create_test_tenant(session_factory)
    admin = {"Authorization": f"Bearer {admin_token}"}
    caller = {
        "Authorization": "Bearer "
        + await _non_admin_token(session_factory, org_id, principal),
    }
    users_before = await _org_user_ids(client, org_id, admin)
    orgs_before = await _org_count(client, admin)

    refused = [
        await client.get(f"/v1/admin/orgs/{org_id}", headers=caller),
        await client.post(
            f"/v1/admin/orgs/{org_id}/users",
            json={
                "email": "added@test.com",
                "display_name": "Added",
                "auth_provider": "database",
                "password": "secret123",
            },
            headers=caller,
        ),
        await client.get(f"/v1/admin/orgs/{org_id}/users", headers=caller),
        await client.post(
            "/v1/admin/orgs", json={"name": "refused-org"}, headers=caller,
        ),
        await client.get("/v1/admin/orgs", headers=caller),
    ]
    assert [r.status_code for r in refused] == [403] * 5, [r.text for r in refused]

    # Nothing the refused calls asked for happened.
    assert await _org_user_ids(client, org_id, admin) == users_before
    assert await _org_count(client, admin) == orgs_before


async def _channel_identity_ids(client: AsyncClient, headers: dict) -> set[str]:
    resp = await client.get("/v1/admin/channel-identities", headers=headers)
    assert resp.status_code == 200, resp.text
    return {i["id"] for i in resp.json()}


async def test_admin_channel_identity_roundtrip(client: AsyncClient, session_factory):
    """An admin links, lists and unlinks a platform identity."""
    org_id, user_id, token, _ = await _create_test_tenant(session_factory)
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/v1/admin/channel-identities",
        json={
            "user_id": str(user_id),
            "platform": "slack",
            "platform_user_id": "U0123ABCDEF",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    identity_id = created.json()["id"]
    assert await _channel_identity_ids(client, headers) == {identity_id}

    listed = await client.get(
        "/v1/admin/channel-identities?platform=slack", headers=headers,
    )
    assert [i["platform_user_id"] for i in listed.json()] == ["U0123ABCDEF"]

    deleted = await client.delete(
        f"/v1/admin/channel-identities/{identity_id}", headers=headers,
    )
    assert deleted.status_code == 204
    assert await _channel_identity_ids(client, headers) == set()


@pytest.mark.parametrize("principal", _NON_ADMIN_PRINCIPALS)
async def test_admin_channel_identity_routes_refuse_non_admin_principals(
    client: AsyncClient, session_factory, principal: str
):
    """Every verb of the channel-identity routes is 403 without the admin permission."""
    org_id, admin_user_id, admin_token, _ = await _create_test_tenant(session_factory)
    admin = {"Authorization": f"Bearer {admin_token}"}
    member_id = await create_user(session_factory, org_id)
    seeded = await client.post(
        "/v1/admin/channel-identities",
        json={
            "user_id": str(member_id),
            "platform": "slack",
            "platform_user_id": "U-SEEDED",
        },
        headers=admin,
    )
    assert seeded.status_code == 201, seeded.text
    seeded_id = seeded.json()["id"]
    caller = {
        "Authorization": "Bearer "
        + await _non_admin_token(session_factory, org_id, principal),
    }

    refused = [
        await client.post(
            "/v1/admin/channel-identities",
            json={
                "user_id": str(admin_user_id),
                "platform": "slack",
                "platform_user_id": "U-NEW",
            },
            headers=caller,
        ),
        await client.get("/v1/admin/channel-identities", headers=caller),
        await client.delete(
            f"/v1/admin/channel-identities/{seeded_id}", headers=caller,
        ),
    ]
    assert [r.status_code for r in refused] == [403] * 3, [r.text for r in refused]

    # No identity appeared and the seeded one is still there.
    assert await _channel_identity_ids(client, admin) == {seeded_id}


@pytest.mark.parametrize("principal", _NON_ADMIN_PRINCIPALS)
async def test_admin_service_account_routes_refuse_non_admin_principals(
    client: AsyncClient, session_factory, principal: str
):
    """Every verb of the admin service-account router is 403 without the admin permission."""
    org_id, _, admin_token, _ = await _create_test_tenant(session_factory)
    admin = {"Authorization": f"Bearer {admin_token}"}
    seeded = await client.post(
        "/v1/admin/service-accounts",
        json={"org_id": str(org_id), "name": "pipeline"},
        headers=admin,
    )
    assert seeded.status_code == 201, seeded.text
    seeded_id = seeded.json()["id"]
    caller = {
        "Authorization": "Bearer "
        + await _non_admin_token(session_factory, org_id, principal),
    }
    accounts_before = await client.get(
        f"/v1/admin/service-accounts?org_id={org_id}", headers=admin,
    )

    refused = [
        await client.post(
            "/v1/admin/service-accounts",
            json={"org_id": str(org_id), "name": "minted"},
            headers=caller,
        ),
        await client.get(
            f"/v1/admin/service-accounts?org_id={org_id}", headers=caller,
        ),
        await client.delete(
            f"/v1/admin/service-accounts/{seeded_id}", headers=caller,
        ),
    ]
    assert [r.status_code for r in refused] == [403] * 3, [r.text for r in refused]

    # Nothing was issued, and the seeded account is not revoked.
    after = await client.get(
        f"/v1/admin/service-accounts?org_id={org_id}", headers=admin,
    )
    assert after.json() == accounts_before.json()
    assert after.json()["service_accounts"][0]["revoked_at"] is None


# ---------------------------------------------------------------------------
# Scheduled run sessions
# ---------------------------------------------------------------------------


async def test_scheduled_run_session_rejects_manual_message(
    app, client: AsyncClient, session_factory
):
    """Loop child sessions are transcript/workspace records, not live chats."""
    org_id, user_id, token, _ = await _create_test_tenant(session_factory)
    session = await _create_scheduled_run_session(
        app, session_factory, org_id, user_id
    )

    resp = await client.post(
        f"/v1/sessions/{session.id}/messages",
        json={"content": "continue manually"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 409
    assert "read-only" in resp.json()["detail"]


async def test_scheduled_run_session_rejects_retry_and_delete(
    app, client: AsyncClient, session_factory
):
    """Users must not mutate a scheduled child session into an ad-hoc run."""
    org_id, user_id, token, _ = await _create_test_tenant(session_factory)
    session = await _create_scheduled_run_session(
        app, session_factory, org_id, user_id
    )
    store = SessionStore(session_factory)
    await store.update_session_status(session.id, "failed")

    retry_resp = await client.post(
        f"/v1/sessions/{session.id}/retry",
        headers={"Authorization": f"Bearer {token}"},
    )
    delete_resp = await client.delete(
        f"/v1/sessions/{session.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert retry_resp.status_code == 409
    assert "read-only" in retry_resp.json()["detail"]
    assert delete_resp.status_code == 409
    assert "read-only" in delete_resp.json()["detail"]


async def test_scheduled_run_session_rejects_workspace_writes(
    app, client: AsyncClient, session_factory
):
    """Scheduled run workspaces are inspectable but user read-only."""
    org_id, user_id, token, _ = await _create_test_tenant(session_factory)
    session = await _create_scheduled_run_session(
        app, session_factory, org_id, user_id
    )
    bucket = app.state.settings.storage.bucket
    await app.state.storage.write(
        bucket,
        session_workspace_key(session.id, "note.txt"),
        b"existing",
    )

    tree_resp = await client.get(
        f"/v1/sessions/{session.id}/workspace/tree",
        headers={"Authorization": f"Bearer {token}"},
    )
    file_resp = await client.get(
        f"/v1/sessions/{session.id}/workspace/file",
        params={"path": "note.txt"},
        headers={"Authorization": f"Bearer {token}"},
    )
    download_resp = await client.get(
        f"/v1/sessions/{session.id}/workspace/download",
        params={"path": "note.txt"},
        headers={"Authorization": f"Bearer {token}"},
    )

    upload_resp = await client.post(
        f"/v1/sessions/{session.id}/workspace/upload",
        files={"file": ("upload.txt", b"new content", "text/plain")},
        headers={"Authorization": f"Bearer {token}"},
    )
    delete_resp = await client.delete(
        f"/v1/sessions/{session.id}/workspace/file",
        params={"path": "note.txt"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert tree_resp.status_code == 200
    assert file_resp.status_code == 200
    assert download_resp.status_code == 200
    assert upload_resp.status_code == 409
    assert "read-only" in upload_resp.json()["detail"]
    assert delete_resp.status_code == 409
    assert "read-only" in delete_resp.json()["detail"]


async def test_scheduled_run_session_rejects_ask_user_question_response(
    app, client: AsyncClient, session_factory
):
    """Ask-user-question answers are also interactive user input and remain blocked."""
    org_id, user_id, token, _ = await _create_test_tenant(session_factory)
    session = await _create_scheduled_run_session(
        app, session_factory, org_id, user_id
    )

    resp = await client.post(
        f"/v1/sessions/{session.id}/ask_user_question/tool-1/respond",
        json={
            "responses": [
                {"question": "Proceed?", "answer": "yes", "is_other": False},
            ],
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 409
    assert "read-only" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# A chat proxy's typed reply answering the open question (answers_open_question)
# ---------------------------------------------------------------------------

_TINY_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA"
    "60e6kgAAAABJRU5ErkJggg=="
)


async def _proxy_session(client, session_factory):
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(session_factory, org_id, name="ops-chat")
    headers = {"Authorization": f"Bearer {issued.token}"}
    created = await client.post(
        "/v1/api/sessions", json={"config": {"source": "ops"}}, headers=headers,
    )
    assert created.status_code == 201, created.text
    return UUID(created.json()["id"]), headers


async def _open_question(session_factory, session_id, tool_call_id="tc-proxy"):
    await SessionStore(session_factory).emit_event(
        session_id,
        EventType.INBOX_INPUT_REQUIRED,
        {
            "tool_call_id": tool_call_id,
            "questions": [{"prompt": "Which region?"}],
            "context": "",
        },
    )


async def _proxy_session_with_open_question(client, session_factory):
    sid, headers = await _proxy_session(client, session_factory)
    await _open_question(session_factory, sid)
    return sid, headers


async def _question_state(session_factory, session_id):
    """(inbox status or None, response events, user.message events)."""
    async with session_factory() as db:
        inbox = (await db.execute(
            select(InboxItem.status).where(InboxItem.session_id == session_id)
        )).scalar_one_or_none()
        types = (await db.execute(
            select(Event.type).where(Event.session_id == session_id)
        )).scalars().all()
    return (
        inbox,
        types.count(EventType.ASK_USER_QUESTION_RESPONSE.value),
        types.count(EventType.USER_MESSAGE.value),
    )


async def test_api_message_with_flag_answers_the_open_question(
    client: AsyncClient, session_factory
):
    sid, headers = await _proxy_session_with_open_question(client, session_factory)
    resp = await client.post(
        f"/v1/api/sessions/{sid}/messages",
        json={"content": "eu-west", "answers_open_question": True},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    assert await _question_state(session_factory, sid) == ("responded", 1, 0)


@pytest.mark.parametrize(
    "payload",
    [
        {"content": "eu-west"},
        {
            "content": "eu-west",
            "answers_open_question": True,
            "images": [{"data": _TINY_PNG, "mime_type": "image/png"}],
        },
        {"content": "/compress", "answers_open_question": True},
    ],
    ids=["no-flag", "image", "builtin-command"],
)
async def test_api_message_that_is_not_a_plain_reply_is_a_normal_message(
    client: AsyncClient, session_factory, payload
):
    sid, headers = await _proxy_session_with_open_question(client, session_factory)
    resp = await client.post(
        f"/v1/api/sessions/{sid}/messages", json=payload, headers=headers,
    )
    assert resp.status_code == 202, resp.text
    assert await _question_state(session_factory, sid) == ("pending", 0, 1)


async def test_api_message_with_flag_and_attachment_is_not_an_answer(
    client: AsyncClient, session_factory
):
    sid, headers = await _proxy_session_with_open_question(client, session_factory)
    resp = await client.post(
        f"/v1/api/sessions/{sid}/messages",
        json={
            "content": "eu-west",
            "answers_open_question": True,
            "attachments": [
                {"path": "missing.txt", "filename": "missing.txt",
                 "mime_type": "text/plain", "size": 1},
            ],
        },
        headers=headers,
    )
    # Reaches attachment resolution (and fails there) instead of answering.
    assert resp.status_code == 422, resp.text
    assert await _question_state(session_factory, sid) == ("pending", 0, 0)


async def test_api_message_with_flag_and_no_pending_question_is_normal(
    client: AsyncClient, session_factory
):
    sid, headers = await _proxy_session(client, session_factory)
    resp = await client.post(
        f"/v1/api/sessions/{sid}/messages",
        json={"content": "hi", "answers_open_question": True},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    assert await _question_state(session_factory, sid) == (None, 0, 1)


async def test_api_message_with_flag_and_stale_question_is_normal(
    client: AsyncClient, session_factory
):
    sid, headers = await _proxy_session_with_open_question(client, session_factory)
    async with session_factory() as db:
        await db.execute(
            update(InboxItem)
            .where(InboxItem.session_id == sid)
            .values(created_at=datetime.now(timezone.utc) - timedelta(hours=2))
        )
        await db.commit()
    resp = await client.post(
        f"/v1/api/sessions/{sid}/messages",
        json={"content": "eu-west", "answers_open_question": True},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    assert await _question_state(session_factory, sid) == ("pending", 0, 1)


async def test_web_message_still_answers_the_open_question(
    client: AsyncClient, session_factory
):
    _, _, token, _ = await _create_test_tenant(session_factory)
    headers = {"Authorization": f"Bearer {token}"}
    created = await client.post(
        "/v1/sessions", json={"model": "gpt-4o"}, headers=headers,
    )
    sid = UUID(created.json()["id"])
    await _open_question(session_factory, sid, "tc-web")
    resp = await client.post(
        f"/v1/sessions/{sid}/messages", json={"content": "this one"}, headers=headers,
    )
    assert resp.status_code == 202, resp.text
    assert await _question_state(session_factory, sid) == ("responded", 1, 0)


async def test_api_message_with_flag_on_a_paused_session_is_a_normal_message(
    client: AsyncClient, session_factory
):
    sid, headers = await _proxy_session_with_open_question(client, session_factory)
    await SessionStore(session_factory).update_session_status(sid, "paused")
    resp = await client.post(
        f"/v1/api/sessions/{sid}/messages",
        json={"content": "eu-west", "answers_open_question": True},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    assert await _question_state(session_factory, sid) == ("pending", 0, 1)


async def test_api_message_with_flag_and_blank_content_is_not_an_answer(
    client: AsyncClient, session_factory
):
    sid, headers = await _proxy_session_with_open_question(client, session_factory)
    resp = await client.post(
        f"/v1/api/sessions/{sid}/messages",
        json={"content": "   ", "answers_open_question": True},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    assert await _question_state(session_factory, sid) == ("pending", 0, 1)
