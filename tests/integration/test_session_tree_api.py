"""Integration tests for /v1/sessions/{id}/tree and /children."""

from __future__ import annotations

import os
import uuid
from uuid import UUID

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from surogates.session.store import SessionStore
from surogates.storage.backend import LocalBackend
from surogates.tenant.auth.jwt import create_access_token
from surogates.tenant.credentials import CredentialVault

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# The agent_id that the API-layer authorization expects on sessions.  We
# make session creation and the app settings use the same value so
# ``_get_session_for_tenant`` accepts our test sessions.
_AGENT_ID = "test-agent"


@pytest_asyncio.fixture(loop_scope="session")
async def app(session_factory, redis_client, pg_url, redis_url, tmp_path_factory):
    """FastAPI app wired to the test containers."""
    os.environ["SUROGATES_DB_URL"] = pg_url
    os.environ["SUROGATES_REDIS_URL"] = redis_url
    os.environ["SUROGATES_AGENT_ID"] = _AGENT_ID

    from surogates.api.app import create_app
    from surogates.config import Settings

    application = create_app()
    application.state.session_factory = session_factory
    application.state.redis = redis_client
    application.state.session_store = SessionStore(session_factory)
    application.state.settings = Settings()
    # Guarantee the expected agent_id regardless of env bleed.
    application.state.settings.agent_id = _AGENT_ID

    storage_root = tmp_path_factory.mktemp("tree-api-storage")
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
    org_id = await create_org(session_factory)
    user_id = uuid.uuid4()
    await create_user(session_factory, org_id, user_id=user_id)
    token = create_access_token(
        org_id, user_id,
        {"sessions:read", "sessions:write", "tools:read", "admin"},
    )
    return org_id, user_id, token


# ---------------------------------------------------------------------------
# /v1/sessions/{id}/tree
# ---------------------------------------------------------------------------


async def test_tree_returns_root_only_when_no_children(
    client: AsyncClient, session_factory, session_store,
):
    org_id, user_id, token = await _tenant(session_factory)
    root = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
    )

    resp = await client.get(
        f"/v1/sessions/{root.id}/tree",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["nodes"][0]["id"] == str(root.id)
    assert data["nodes"][0]["depth"] == 0
    assert data["nodes"][0]["parent_id"] is None
    assert data["nodes"][0]["root_session_id"] == str(root.id)


async def test_tree_returns_nested_descendants(
    client: AsyncClient, session_factory, session_store,
):
    org_id, user_id, token = await _tenant(session_factory)

    root = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
        config={"coordinator": True},
    )
    child1 = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
        parent_id=root.id, channel="worker",
        config={"agent_type": "researcher"},
    )
    child2 = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
        parent_id=root.id, channel="worker",
    )
    grandchild = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
        parent_id=child1.id, channel="delegation",
        config={"agent_type": "analyzer"},
    )

    resp = await client.get(
        f"/v1/sessions/{root.id}/tree",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 4

    by_id = {n["id"]: n for n in data["nodes"]}
    assert by_id[str(root.id)]["depth"] == 0
    assert by_id[str(child1.id)]["depth"] == 1
    assert by_id[str(child2.id)]["depth"] == 1
    assert by_id[str(grandchild.id)]["depth"] == 2

    # agent_type flows through to the response.
    assert by_id[str(child1.id)]["agent_type"] == "researcher"
    assert by_id[str(child2.id)]["agent_type"] is None
    assert by_id[str(grandchild.id)]["agent_type"] == "analyzer"

    # Every node shares the root's root_session_id.
    for n in data["nodes"]:
        assert n["root_session_id"] == str(root.id)


async def test_tree_authorization_blocks_other_tenant(
    client: AsyncClient, session_factory, session_store,
):
    org_a, user_a, _token_a = await _tenant(session_factory)
    _, _, token_b = await _tenant(session_factory)

    root = await session_store.create_session(
        user_id=user_a, org_id=org_a, agent_id=_AGENT_ID,
    )
    resp = await client.get(
        f"/v1/sessions/{root.id}/tree",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp.status_code == 404


async def test_tree_unknown_session_returns_404(
    client: AsyncClient, session_factory,
):
    _, _, token = await _tenant(session_factory)
    resp = await client.get(
        f"/v1/sessions/{uuid.uuid4()}/tree",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /v1/sessions/{id}/children
# ---------------------------------------------------------------------------


async def test_children_returns_direct_descendants_only(
    client: AsyncClient, session_factory, session_store,
):
    org_id, user_id, token = await _tenant(session_factory)

    root = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
    )
    child = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
        parent_id=root.id, channel="worker",
    )
    # Grandchild — should NOT appear in root's children response.
    await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
        parent_id=child.id, channel="delegation",
    )

    resp = await client.get(
        f"/v1/sessions/{root.id}/children",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["children"][0]["id"] == str(child.id)
    assert data["children"][0]["parent_id"] == str(root.id)


async def test_children_returns_agent_type(
    client: AsyncClient, session_factory, session_store,
):
    org_id, user_id, token = await _tenant(session_factory)
    root = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
    )
    await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
        parent_id=root.id, channel="worker",
        config={"agent_type": "experiment-runner"},
    )

    resp = await client.get(
        f"/v1/sessions/{root.id}/children",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    children = resp.json()["children"]
    assert len(children) == 1
    assert children[0]["agent_type"] == "experiment-runner"


async def test_children_empty_when_no_descendants(
    client: AsyncClient, session_factory, session_store,
):
    org_id, user_id, token = await _tenant(session_factory)
    root = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
    )

    resp = await client.get(
        f"/v1/sessions/{root.id}/children",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"children": [], "total": 0}


async def test_children_authorization_blocks_other_tenant(
    client: AsyncClient, session_factory, session_store,
):
    org_a, user_a, _ = await _tenant(session_factory)
    _, _, token_b = await _tenant(session_factory)
    root = await session_store.create_session(
        user_id=user_a, org_id=org_a, agent_id=_AGENT_ID,
    )

    resp = await client.get(
        f"/v1/sessions/{root.id}/children",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp.status_code == 404
