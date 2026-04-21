"""Integration tests for the /v1/agents REST API."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from surogates.db.models import Agent
from surogates.session.store import SessionStore
from surogates.storage.backend import LocalBackend
from surogates.tenant.auth.jwt import create_access_token
from surogates.tenant.credentials import CredentialVault

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def app(session_factory, redis_client, pg_url, redis_url, tmp_path_factory):
    """FastAPI app wired to the test containers."""
    os.environ["SUROGATES_DB_URL"] = pg_url
    os.environ["SUROGATES_REDIS_URL"] = redis_url

    from surogates.api.app import create_app
    from surogates.config import Settings

    application = create_app()
    application.state.session_factory = session_factory
    application.state.redis = redis_client
    application.state.session_store = SessionStore(session_factory)
    application.state.settings = Settings()

    # Use an isolated tmp directory for storage so tests don't collide with
    # real filesystem state at /tmp/surogates/tenant-assets.
    storage_root = tmp_path_factory.mktemp("agents-api-storage")
    application.state.storage = LocalBackend(base_path=str(storage_root))
    application.state.credential_vault = CredentialVault(
        session_factory, Fernet.generate_key(),
    )
    return application


@pytest_asyncio.fixture(loop_scope="session")
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        yield c


async def _tenant(session_factory) -> tuple[UUID, UUID, str]:
    """Create org + user and return (org_id, user_id, jwt)."""
    org_id = await create_org(session_factory)
    user_id = uuid.uuid4()
    await create_user(session_factory, org_id, user_id=user_id)
    token = create_access_token(
        org_id, user_id,
        {"sessions:read", "sessions:write", "tools:read", "admin"},
    )
    return org_id, user_id, token


def _valid_agent_md(
    *,
    name: str,
    description: str = "Reviews code",
    body: str = "You are a code reviewer.",
    extra_fm: str = "",
) -> str:
    lines = ["---", f"name: {name}", f"description: {description}"]
    if extra_fm:
        lines.append(extra_fm.rstrip())
    lines.append("---")
    lines.append(body)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# GET /v1/agents
# ---------------------------------------------------------------------------


async def test_list_agents_empty(client: AsyncClient, session_factory):
    _, _, token = await _tenant(session_factory)
    resp = await client.get(
        "/v1/agents",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data == {"agents": [], "total": 0}


async def test_list_agents_returns_created_agent(
    client: AsyncClient, session_factory,
):
    _, _, token = await _tenant(session_factory)
    create = await client.post(
        "/v1/agents",
        json={
            "name": "code-reviewer",
            "content": _valid_agent_md(
                name="code-reviewer",
                extra_fm="model: claude-sonnet-4-6\nmax_iterations: 20\npolicy_profile: read_only",
            ),
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert create.status_code == 201

    resp = await client.get(
        "/v1/agents",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    entry = data["agents"][0]
    assert entry["name"] == "code-reviewer"
    assert entry["source"] == "user"
    assert entry["model"] == "claude-sonnet-4-6"
    assert entry["max_iterations"] == 20
    assert entry["policy_profile"] == "read_only"
    assert entry["enabled"] is True


async def test_list_agents_includes_db_overlay(
    client: AsyncClient, session_factory,
):
    """An org-DB row is visible in the list response."""
    org_id, user_id, token = await _tenant(session_factory)

    # Insert an org-wide DB row directly.
    async with session_factory() as db:
        db.add(Agent(
            id=uuid.uuid4(),
            org_id=org_id,
            user_id=None,
            name="admin-researcher",
            description="Research agent (org admin override)",
            system_prompt="Body",
            config={
                "model": "claude-opus-4-7",
                "max_iterations": 50,
            },
        ))
        await db.commit()

    resp = await client.get(
        "/v1/agents",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    names = {a["name"] for a in resp.json()["agents"]}
    assert "admin-researcher" in names


# ---------------------------------------------------------------------------
# GET /v1/agents/{name}
# ---------------------------------------------------------------------------


async def test_get_agent_returns_full_detail(
    client: AsyncClient, session_factory,
):
    _, _, token = await _tenant(session_factory)
    content = _valid_agent_md(
        name="researcher",
        description="Investigate topics",
        body="You are a research agent.",
        extra_fm=(
            "tools: [read_file, search_files]\n"
            "disallowed_tools: [write_file]\n"
            "model: claude-sonnet-4-6\n"
            "max_iterations: 15"
        ),
    )
    await client.post(
        "/v1/agents",
        json={"name": "researcher", "content": content},
        headers={"Authorization": f"Bearer {token}"},
    )

    resp = await client.get(
        "/v1/agents/researcher",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["name"] == "researcher"
    assert detail["description"] == "Investigate topics"
    assert detail["tools"] == ["read_file", "search_files"]
    assert detail["disallowed_tools"] == ["write_file"]
    assert detail["model"] == "claude-sonnet-4-6"
    assert detail["max_iterations"] == 15
    assert detail["source"] == "user"
    assert "You are a research agent." in detail["system_prompt"]


async def test_get_agent_unknown_returns_404(
    client: AsyncClient, session_factory,
):
    _, _, token = await _tenant(session_factory)
    resp = await client.get(
        "/v1/agents/nope",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /v1/agents
# ---------------------------------------------------------------------------


async def test_create_agent_succeeds(client: AsyncClient, session_factory):
    _, _, token = await _tenant(session_factory)
    resp = await client.post(
        "/v1/agents",
        json={"name": "fresh", "content": _valid_agent_md(name="fresh")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    assert resp.json()["success"] is True


async def test_create_agent_duplicate_returns_409(
    client: AsyncClient, session_factory,
):
    _, _, token = await _tenant(session_factory)
    headers = {"Authorization": f"Bearer {token}"}
    await client.post(
        "/v1/agents",
        json={"name": "dup", "content": _valid_agent_md(name="dup")},
        headers=headers,
    )
    resp = await client.post(
        "/v1/agents",
        json={"name": "dup", "content": _valid_agent_md(name="dup")},
        headers=headers,
    )
    assert resp.status_code == 409


async def test_create_agent_empty_name_returns_422(
    client: AsyncClient, session_factory,
):
    _, _, token = await _tenant(session_factory)
    resp = await client.post(
        "/v1/agents",
        json={"name": "   ", "content": _valid_agent_md(name="x")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_create_agent_with_path_traversal_returns_422(
    client: AsyncClient, session_factory,
):
    _, _, token = await _tenant(session_factory)
    resp = await client.post(
        "/v1/agents",
        json={"name": "../evil", "content": _valid_agent_md(name="evil")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_create_agent_missing_frontmatter_returns_422(
    client: AsyncClient, session_factory,
):
    _, _, token = await _tenant(session_factory)
    resp = await client.post(
        "/v1/agents",
        json={"name": "bad", "content": "No frontmatter here."},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_create_agent_name_mismatch_returns_422(
    client: AsyncClient, session_factory,
):
    """Request name and frontmatter name must agree or the agent becomes
    invisible to delete/view (storage path vs catalog listing mismatch)."""
    _, _, token = await _tenant(session_factory)
    mismatched = _valid_agent_md(name="frontmatter-name")
    resp = await client.post(
        "/v1/agents",
        json={"name": "request-name", "content": mismatched},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
    assert "frontmatter" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# PUT /v1/agents/{name}
# ---------------------------------------------------------------------------


async def test_edit_agent_replaces_content(
    client: AsyncClient, session_factory,
):
    _, _, token = await _tenant(session_factory)
    headers = {"Authorization": f"Bearer {token}"}
    await client.post(
        "/v1/agents",
        json={"name": "upd", "content": _valid_agent_md(name="upd", body="v1")},
        headers=headers,
    )

    resp = await client.put(
        "/v1/agents/upd",
        json={"content": _valid_agent_md(name="upd", body="v2 body")},
        headers=headers,
    )
    assert resp.status_code == 200

    detail = await client.get("/v1/agents/upd", headers=headers)
    assert "v2 body" in detail.json()["system_prompt"]


async def test_edit_unknown_agent_returns_404(
    client: AsyncClient, session_factory,
):
    _, _, token = await _tenant(session_factory)
    resp = await client.put(
        "/v1/agents/nope",
        json={"content": _valid_agent_md(name="nope")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /v1/agents/{name}
# ---------------------------------------------------------------------------


async def test_delete_agent_succeeds(client: AsyncClient, session_factory):
    _, _, token = await _tenant(session_factory)
    headers = {"Authorization": f"Bearer {token}"}
    await client.post(
        "/v1/agents",
        json={"name": "to-delete", "content": _valid_agent_md(name="to-delete")},
        headers=headers,
    )

    resp = await client.delete("/v1/agents/to-delete", headers=headers)
    assert resp.status_code == 204

    # Subsequent GET returns 404.
    missing = await client.get("/v1/agents/to-delete", headers=headers)
    assert missing.status_code == 404


async def test_delete_unknown_agent_returns_404(
    client: AsyncClient, session_factory,
):
    _, _, token = await _tenant(session_factory)
    resp = await client.delete(
        "/v1/agents/nope",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


async def test_agents_are_scoped_to_tenant(
    client: AsyncClient, session_factory,
):
    """A user in one org cannot see agents created by another org."""
    _, _, token_a = await _tenant(session_factory)
    _, _, token_b = await _tenant(session_factory)

    await client.post(
        "/v1/agents",
        json={"name": "org-a-only", "content": _valid_agent_md(name="org-a-only")},
        headers={"Authorization": f"Bearer {token_a}"},
    )

    resp_b = await client.get(
        "/v1/agents",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    names_b = {a["name"] for a in resp_b.json()["agents"]}
    assert "org-a-only" not in names_b
