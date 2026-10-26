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
from surogates.storage.tenant import TenantStorage
from surogates.tenant.auth.jwt import create_access_token
from surogates.tenant.credentials import CredentialVault

from .conftest import OrgPrincipals, create_org, create_user, org_principals

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
    application.state.settings.storage.bucket = f"test-agent-{uuid.uuid4()}"

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
                extra_fm="model: claude-sonnet-4-6\nmax_iterations: 20",
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


# ---------------------------------------------------------------------------
# Sub-agents in the org-shared layer
# ---------------------------------------------------------------------------
#
# A sub-agent found by name resolves to the caller's own layer first and to
# the org-shared layer second.  Changing an org-shared one needs an org-wide
# service account (one bound to no agent) or an ``admin`` token; an end-user
# changes only their own, and a service account bound to one agent (a
# customer's API key) changes none in the org-shared layer.  A sub-agent
# created by a caller with no user lands in the org-shared layer, so that needs
# the same standing.


@pytest_asyncio.fixture(loop_scope="session")
async def principals(session_factory) -> OrgPrincipals:
    return await org_principals(session_factory)


def _storage(app, org_id: UUID, user_id: UUID | None) -> TenantStorage:
    return TenantStorage(
        backend=app.state.storage,
        org_id=org_id,
        user_id=user_id,
        bucket=app.state.settings.storage.bucket,
    )


async def _agent_file(ts: TenantStorage, name: str) -> str | None:
    """The AGENT.md the storage finds under *name*, or ``None`` when there is none."""
    found = await ts.agent_exists(name)
    return None if found is None else await ts.read_agent(found["key_prefix"])


def _edit(client: AsyncClient, name: str, headers: dict, prefix: str = "/v1"):
    return client.put(
        f"{prefix}/agents/{name}",
        json={"content": _valid_agent_md(name=name, body="rewritten")},
        headers=headers,
    )


def _delete(client: AsyncClient, name: str, headers: dict, prefix: str = "/v1"):
    return client.delete(f"{prefix}/agents/{name}", headers=headers)


async def test_an_end_user_cannot_edit_an_org_shared_sub_agent(
    app, client: AsyncClient, principals,
):
    shared = _storage(app, principals.org_id, None)
    await shared.write_agent("shared-edit", _valid_agent_md(name="shared-edit"))
    before = await _agent_file(shared, "shared-edit")

    resp = await _edit(client, "shared-edit", principals.auth("user"))

    assert resp.status_code == 403, resp.text
    assert await _agent_file(shared, "shared-edit") == before


async def test_an_end_user_cannot_delete_an_org_shared_sub_agent(
    app, client: AsyncClient, principals,
):
    shared = _storage(app, principals.org_id, None)
    await shared.write_agent("shared-delete", _valid_agent_md(name="shared-delete"))
    before = await _agent_file(shared, "shared-delete")

    resp = await _delete(client, "shared-delete", principals.auth("user"))

    assert resp.status_code == 403, resp.text
    assert before is not None
    assert await _agent_file(shared, "shared-delete") == before


@pytest.mark.parametrize(
    ("principal", "prefix"),
    [
        ("admin", "/v1"),
        ("service_account_session", "/v1"),
        ("service_account", "/v1/api"),
    ],
)
async def test_a_service_account_or_an_admin_can_edit_and_delete_an_org_shared_sub_agent(
    app, client: AsyncClient, principals, principal: str, prefix: str,
):
    shared = _storage(app, principals.org_id, None)
    name = f"shared-{principal}".replace("_", "-")
    await shared.write_agent(name, _valid_agent_md(name=name))
    headers = principals.auth(principal)

    edited = await _edit(client, name, headers, prefix)
    assert edited.status_code == 200, edited.text
    assert "rewritten" in await _agent_file(shared, name)

    deleted = await _delete(client, name, headers, prefix)
    assert deleted.status_code == 204, deleted.text
    assert await _agent_file(shared, name) is None


@pytest.mark.parametrize(
    ("principal", "prefix"),
    [
        ("bound_service_account_session", "/v1"),
        ("bound_service_account", "/v1/api"),
    ],
)
async def test_an_agent_bound_service_account_cannot_edit_or_delete_an_org_shared_sub_agent(
    app, client: AsyncClient, principals, principal: str, prefix: str,
):
    shared = _storage(app, principals.org_id, None)
    name = f"shared-{principal}".replace("_", "-")
    await shared.write_agent(name, _valid_agent_md(name=name))
    before = await _agent_file(shared, name)
    headers = principals.auth(principal)

    edited = await _edit(client, name, headers, prefix)
    assert edited.status_code == 403, edited.text
    assert await _agent_file(shared, name) == before

    deleted = await _delete(client, name, headers, prefix)
    assert deleted.status_code == 403, deleted.text
    assert await _agent_file(shared, name) == before


def _create(client: AsyncClient, name: str, headers: dict, prefix: str = "/v1"):
    return client.post(
        f"{prefix}/agents",
        json={"name": name, "content": _valid_agent_md(name=name)},
        headers=headers,
    )


@pytest.mark.parametrize(
    ("principal", "prefix"),
    [("service_account_session", "/v1"), ("service_account", "/v1/api")],
)
async def test_an_org_wide_service_account_creates_a_sub_agent_in_the_org_shared_layer(
    app, client: AsyncClient, principals, principal: str, prefix: str,
):
    name = f"made-{principal}".replace("_", "-")

    resp = await _create(client, name, principals.auth(principal), prefix)

    assert resp.status_code == 201, resp.text
    found = await _storage(app, principals.org_id, None).agent_exists(name)
    assert found is not None and found["layer"] == "org"


async def test_a_case_type_accounts_session_creates_a_sub_agent_in_the_org_shared_layer(
    app, client: AsyncClient, principals, session_factory,
):
    from surogates.tenant.auth.jwt import create_service_account_session_token
    from surogates.tenant.auth.service_account import ServiceAccountStore

    account_id = await ServiceAccountStore(session_factory).case_type_account(
        principals.org_id, "claims",
    )
    token = create_service_account_session_token(principals.org_id, account_id, uuid.uuid4())

    resp = await _create(client, "made-by-keel", {"Authorization": f"Bearer {token}"})

    assert resp.status_code == 201, resp.text
    found = await _storage(app, principals.org_id, None).agent_exists("made-by-keel")
    assert found is not None and found["layer"] == "org"


@pytest.mark.parametrize(
    ("principal", "prefix"),
    [
        ("bound_service_account_session", "/v1"),
        ("bound_service_account", "/v1/api"),
        ("channel_session", "/v1"),
    ],
)
async def test_a_caller_with_no_standing_cannot_create_a_sub_agent_in_the_org_shared_layer(
    app, client: AsyncClient, principals, principal: str, prefix: str,
):
    name = f"refused-{principal}".replace("_", "-")

    resp = await _create(client, name, principals.auth(principal), prefix)

    assert resp.status_code == 403, resp.text
    assert await _storage(app, principals.org_id, None).agent_exists(name) is None


@pytest.mark.parametrize(("principal", "owner"), [("user", "user_id"), ("admin", "admin_id")])
async def test_a_signed_in_user_creates_a_sub_agent_in_their_own_layer(
    app, client: AsyncClient, principals, principal: str, owner: str,
):
    name = f"made-by-{principal}"

    resp = await _create(client, name, principals.auth(principal))

    assert resp.status_code == 201, resp.text
    found = await _storage(app, principals.org_id, getattr(principals, owner)).agent_exists(name)
    assert found is not None and found["layer"] == "user"
    assert await _storage(app, principals.org_id, None).agent_exists(name) is None


async def test_a_user_copy_and_an_org_shared_copy_of_one_name_are_deleted_one_at_a_time(
    app, client: AsyncClient, principals,
):
    name = "twice"
    shared = _storage(app, principals.org_id, None)
    own = _storage(app, principals.org_id, principals.user_id)
    await shared.write_agent(name, _valid_agent_md(name=name, body="shared"))
    await own.write_agent(name, _valid_agent_md(name=name, body="mine"))
    shared_before = await _agent_file(shared, name)
    headers = principals.auth("user")

    first = await _delete(client, name, headers)
    assert first.status_code == 204, first.text
    assert await _agent_file(shared, name) == shared_before
    assert (await own.agent_exists(name))["layer"] == "org"

    second = await _delete(client, name, headers)
    assert second.status_code == 403, second.text
    assert await _agent_file(shared, name) == shared_before


async def test_an_end_user_can_edit_and_delete_their_own_sub_agent(
    app, client: AsyncClient, principals,
):
    own = _storage(app, principals.org_id, principals.user_id)
    await own.write_agent("mine", _valid_agent_md(name="mine"))
    headers = principals.auth("user")

    edited = await _edit(client, "mine", headers)
    assert edited.status_code == 200, edited.text
    assert "rewritten" in await _agent_file(own, "mine")

    deleted = await _delete(client, "mine", headers)
    assert deleted.status_code == 204, deleted.text
    assert await _agent_file(own, "mine") is None
