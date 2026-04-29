"""HTTP-level integration tests for the /v1/kb routes.

Exercises the FastAPI routes through ``httpx.AsyncClient + ASGITransport``
(same pattern as ``test_api.py`` and ``test_website_api.py``) so we
cover the routing layer, dependency injection, request validation, and
status-code semantics — not just the underlying SQL.

Per-agent grants enforcement is also tested here end-to-end via direct
``KbStore.search`` calls (no harness round-trip needed) since the
grants logic is at the store layer.
"""
from __future__ import annotations

import json
import os
import uuid
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from surogates.session.store import SessionStore
from surogates.storage.embeddings import StubEmbeddingClient
from surogates.tenant.auth.jwt import create_access_token

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Fixtures (mirrors test_api.py's app + client; trimmed to what KB routes need)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def app(session_factory, redis_client, pg_url, redis_url, tmp_path_factory):
    """FastAPI app wired to the test containers + a tmp LocalBackend."""
    os.environ["SUROGATES_DB_URL"] = pg_url
    os.environ["SUROGATES_REDIS_URL"] = redis_url

    from surogates.api.app import create_app
    from surogates.config import Settings
    from surogates.storage.backend import LocalBackend
    from surogates.tenant.credentials import CredentialVault
    from cryptography.fernet import Fernet

    application = create_app()
    application.state.session_factory = session_factory
    application.state.redis = redis_client
    application.state.session_store = SessionStore(session_factory)
    application.state.settings = Settings()
    storage_root = tmp_path_factory.mktemp("kb_api_storage")
    application.state.storage = LocalBackend(base_path=str(storage_root))
    application.state.embedder = StubEmbeddingClient(dim=1024)
    application.state.credential_vault = CredentialVault(
        session_factory, Fernet.generate_key(),
    )
    return application


@pytest_asyncio.fixture(loop_scope="session")
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _tenant(session_factory) -> tuple[UUID, UUID, str]:
    """Create org + user + JWT. Returns ``(org_id, user_id, token)``."""
    org_id = await create_org(session_factory)
    user_id = uuid.uuid4()
    await create_user(
        session_factory,
        org_id,
        user_id=user_id,
        email=f"u-{user_id}@test.com",
        password="testpass",
    )
    token = create_access_token(
        org_id, user_id,
        {"sessions:read", "sessions:write", "tools:read", "admin"},
    )
    return org_id, user_id, token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_agent(session_factory, org_id: UUID, name: str = "test-agent") -> UUID:
    agent_id = uuid.uuid4()
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO agents (id, org_id, name) "
                "VALUES (:id, :org_id, :name)"
            ),
            {"id": agent_id, "org_id": org_id, "name": name},
        )
        await db.commit()
    return agent_id


# ---------------------------------------------------------------------------
# CRUD: create + list + get
# ---------------------------------------------------------------------------


async def test_create_list_get_kb(client, session_factory):
    org_id, _, token = await _tenant(session_factory)
    name = f"my-kb-{uuid.uuid4().hex[:8]}"

    r = await client.post(
        "/v1/kb",
        json={"name": name, "description": "test"},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    kb = r.json()
    assert kb["name"] == name
    assert kb["org_id"] == str(org_id)
    assert kb["is_platform"] is False

    r = await client.get("/v1/kb", headers=_auth(token))
    assert r.status_code == 200
    listed = {row["name"] for row in r.json()["kbs"]}
    assert name in listed

    r = await client.get(f"/v1/kb/{kb['id']}", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["name"] == name


async def test_get_kb_404_for_other_org(client, session_factory):
    """One tenant can't fetch another tenant's KB."""
    org_a, _, token_a = await _tenant(session_factory)
    org_b, _, token_b = await _tenant(session_factory)

    r = await client.post(
        "/v1/kb",
        json={"name": f"kb-{uuid.uuid4()}"},
        headers=_auth(token_b),
    )
    kb = r.json()

    r = await client.get(f"/v1/kb/{kb['id']}", headers=_auth(token_a))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Sources: add + list + sync + delete (tombstone)
# ---------------------------------------------------------------------------


async def test_add_list_delete_source(client, session_factory, tmp_path):
    org_id, _, token = await _tenant(session_factory)

    r = await client.post(
        "/v1/kb",
        json={"name": f"kb-{uuid.uuid4().hex[:8]}"},
        headers=_auth(token),
    )
    kb = r.json()

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "intro.md").write_text("# Intro\n\nbody\n")

    r = await client.post(
        f"/v1/kb/{kb['id']}/sources",
        json={"kind": "markdown_dir", "config": {"path": str(docs)}},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    src = r.json()
    assert src["kind"] == "markdown_dir"
    assert src["last_status"] is None  # not yet synced

    r = await client.get(f"/v1/kb/{kb['id']}/sources", headers=_auth(token))
    assert len(r.json()["sources"]) == 1

    # Sync it.
    r = await client.post(
        f"/v1/kb/{kb['id']}/sources/{src['id']}/sync",
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["docs_added"] == 1

    # Tombstone the source.
    r = await client.delete(
        f"/v1/kb/{kb['id']}/sources/{src['id']}",
        headers=_auth(token),
    )
    assert r.status_code == 204

    # Listing now hides the tombstoned source (kb_source.deleted_at filter).
    r = await client.get(f"/v1/kb/{kb['id']}/sources", headers=_auth(token))
    assert r.json()["sources"] == []

    # Re-deleting the same tombstone is 404.
    r = await client.delete(
        f"/v1/kb/{kb['id']}/sources/{src['id']}",
        headers=_auth(token),
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Browse: raw_docs and wiki_entries
# ---------------------------------------------------------------------------


async def test_browse_raw_docs_after_sync(client, session_factory, tmp_path):
    org_id, _, token = await _tenant(session_factory)
    r = await client.post(
        "/v1/kb",
        json={"name": f"kb-{uuid.uuid4().hex[:8]}"},
        headers=_auth(token),
    )
    kb = r.json()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\n\nbody-a\n")
    (docs / "b.md").write_text("# B\n\nbody-b\n")

    r = await client.post(
        f"/v1/kb/{kb['id']}/sources",
        json={"kind": "markdown_dir", "config": {"path": str(docs)}},
        headers=_auth(token),
    )
    src = r.json()
    await client.post(
        f"/v1/kb/{kb['id']}/sources/{src['id']}/sync",
        headers=_auth(token),
    )

    r = await client.get(f"/v1/kb/{kb['id']}/raw", headers=_auth(token))
    assert r.status_code == 200
    payload = r.json()
    assert payload["total"] == 2
    titles = {d["title"] for d in payload["raw_docs"]}
    assert titles == {"A", "B"}


async def test_browse_wiki_entries_after_recompile(client, session_factory, tmp_path):
    org_id, _, token = await _tenant(session_factory)
    r = await client.post(
        "/v1/kb",
        json={"name": f"kb-{uuid.uuid4().hex[:8]}"},
        headers=_auth(token),
    )
    kb = r.json()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\n\nbody\n")
    r = await client.post(
        f"/v1/kb/{kb['id']}/sources",
        json={"kind": "markdown_dir", "config": {"path": str(docs)}},
        headers=_auth(token),
    )
    src = r.json()
    # Sync + chained compile via ?compile=true
    r = await client.post(
        f"/v1/kb/{kb['id']}/sources/{src['id']}/sync?compile=true",
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text

    r = await client.get(f"/v1/kb/{kb['id']}/wiki", headers=_auth(token))
    assert r.status_code == 200
    payload = r.json()
    assert payload["total"] == 1
    assert payload["wiki_entries"][0]["kind"] == "summary"


async def test_recompile_endpoint(client, session_factory, tmp_path):
    org_id, _, token = await _tenant(session_factory)
    r = await client.post(
        "/v1/kb",
        json={"name": f"kb-{uuid.uuid4().hex[:8]}"},
        headers=_auth(token),
    )
    kb = r.json()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\n\nbody\n")
    r = await client.post(
        f"/v1/kb/{kb['id']}/sources",
        json={"kind": "markdown_dir", "config": {"path": str(docs)}},
        headers=_auth(token),
    )
    src = r.json()
    await client.post(
        f"/v1/kb/{kb['id']}/sources/{src['id']}/sync",
        headers=_auth(token),
    )
    # First recompile (no watermark): compiles all 1 raw_doc.
    r = await client.post(
        f"/v1/kb/{kb['id']}/recompile",
        headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["entries_added"] == 1


# ---------------------------------------------------------------------------
# Delete: KB cascade
# ---------------------------------------------------------------------------


async def test_delete_kb_cascades(client, session_factory, tmp_path):
    org_id, _, token = await _tenant(session_factory)
    r = await client.post(
        "/v1/kb",
        json={"name": f"kb-{uuid.uuid4().hex[:8]}"},
        headers=_auth(token),
    )
    kb = r.json()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "x.md").write_text("# X\n\n.\n")
    r = await client.post(
        f"/v1/kb/{kb['id']}/sources",
        json={"kind": "markdown_dir", "config": {"path": str(docs)}},
        headers=_auth(token),
    )
    src = r.json()
    await client.post(
        f"/v1/kb/{kb['id']}/sources/{src['id']}/sync?compile=true",
        headers=_auth(token),
    )

    r = await client.delete(f"/v1/kb/{kb['id']}", headers=_auth(token))
    assert r.status_code == 204

    # Subsequent gets are 404.
    r = await client.get(f"/v1/kb/{kb['id']}", headers=_auth(token))
    assert r.status_code == 404

    # Sources/raw_docs/wiki rows cascaded away.
    async with session_factory() as db:
        n_src = (
            await db.execute(
                text("SELECT count(*) FROM kb_source WHERE kb_id = :id"),
                {"id": kb["id"]},
            )
        ).scalar()
        n_raw = (
            await db.execute(
                text("SELECT count(*) FROM kb_raw_doc WHERE kb_id = :id"),
                {"id": kb["id"]},
            )
        ).scalar()
    assert n_src == 0
    assert n_raw == 0


# ---------------------------------------------------------------------------
# Per-agent grants: HTTP CRUD + enforcement at the KbStore layer
# ---------------------------------------------------------------------------


async def test_grants_crud(client, session_factory):
    org_id, _, token = await _tenant(session_factory)
    agent_id = await _seed_agent(session_factory, org_id)

    r = await client.post(
        "/v1/kb",
        json={"name": f"kb-{uuid.uuid4().hex[:8]}"},
        headers=_auth(token),
    )
    kb = r.json()

    # Initially empty.
    r = await client.get(f"/v1/kb/{kb['id']}/grants", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["grants"] == []

    # Grant.
    r = await client.post(
        f"/v1/kb/{kb['id']}/grants",
        json={"agent_id": str(agent_id)},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text

    # Idempotent grant.
    r = await client.post(
        f"/v1/kb/{kb['id']}/grants",
        json={"agent_id": str(agent_id)},
        headers=_auth(token),
    )
    assert r.status_code == 201
    granted_at_first = r.json()["granted_at"]

    # List shows one.
    r = await client.get(f"/v1/kb/{kb['id']}/grants", headers=_auth(token))
    assert len(r.json()["grants"]) == 1

    # Revoke.
    r = await client.delete(
        f"/v1/kb/{kb['id']}/grants/{agent_id}",
        headers=_auth(token),
    )
    assert r.status_code == 204

    # Revoking again is 404.
    r = await client.delete(
        f"/v1/kb/{kb['id']}/grants/{agent_id}",
        headers=_auth(token),
    )
    assert r.status_code == 404


async def test_grant_for_agent_in_other_org_is_404(client, session_factory):
    """Agent must belong to the same tenant as the KB."""
    org_a, _, token_a = await _tenant(session_factory)
    org_b, _, _ = await _tenant(session_factory)
    other_agent = await _seed_agent(session_factory, org_b)

    r = await client.post(
        "/v1/kb",
        json={"name": f"kb-{uuid.uuid4().hex[:8]}"},
        headers=_auth(token_a),
    )
    kb = r.json()
    r = await client.post(
        f"/v1/kb/{kb['id']}/grants",
        json={"agent_id": str(other_agent)},
        headers=_auth(token_a),
    )
    assert r.status_code == 404


async def test_kbstore_search_grants_enforcement(session_factory, tmp_path):
    """Direct KbStore test of the grant filter: with agent_id set, an
    org KB without a grant for that agent is invisible; with a grant,
    visible.
    """
    from surogates.jobs.kb_ingest import run_ingest
    from surogates.jobs.wiki_compile import compile_wiki_for_kb
    from surogates.storage.backend import LocalBackend
    from surogates.storage.kb_store import KbStore

    storage = LocalBackend(base_path=str(tmp_path / "garage"))
    org_id = await create_org(session_factory)
    agent_id = await _seed_agent(session_factory, org_id)

    # Seed a KB + 1 source + ingest + compile.
    kb_id = uuid.uuid4()
    source_id = uuid.uuid4()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "intro.md").write_text("# Intro\n\nThe word foobarbaz appears here.\n")
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO kb (id, org_id, name, agents_md) "
                "VALUES (:id, :org_id, :name, '')"
            ),
            {"id": kb_id, "org_id": org_id, "name": f"kb-{uuid.uuid4()}"},
        )
        await db.execute(
            text(
                "INSERT INTO kb_source (id, kb_id, kind, config) "
                "VALUES (:id, :kb_id, 'markdown_dir', :config)"
            ),
            {
                "id": source_id,
                "kb_id": kb_id,
                "config": json.dumps({"path": str(docs)}),
            },
        )
        await db.commit()
    await run_ingest(
        source_id,
        session_factory=session_factory,
        storage_backend=storage,
    )
    await compile_wiki_for_kb(
        kb_id,
        session_factory=session_factory,
        storage_backend=storage,
        embedder=None,
    )

    store = KbStore(session_factory)

    # Without agent_id: legacy "all-org-visible" — agent sees the KB.
    hits_legacy = await store.search(
        org_id=org_id, query="foobarbaz", agent_id=None,
    )
    assert hits_legacy, "agent_id=None should see all org KBs"

    # With agent_id but NO grant: KB is invisible.
    hits_blocked = await store.search(
        org_id=org_id, query="foobarbaz", agent_id=agent_id,
    )
    assert hits_blocked == [], "agent without grant must not see org KB"

    # Grant + retry → visible again.
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO agent_kb_grant (agent_id, kb_id) "
                "VALUES (:agent_id, :kb_id)"
            ),
            {"agent_id": agent_id, "kb_id": kb_id},
        )
        await db.commit()
    hits_granted = await store.search(
        org_id=org_id, query="foobarbaz", agent_id=agent_id,
    )
    assert hits_granted, "agent with grant must see org KB"


async def test_kbstore_platform_kb_visible_without_grant(session_factory, tmp_path):
    """Platform KBs (org_id IS NULL) are implicitly granted to every
    agent — no agent_kb_grant row required.
    """
    from surogates.jobs.kb_ingest import run_ingest
    from surogates.jobs.wiki_compile import compile_wiki_for_kb
    from surogates.storage.backend import LocalBackend
    from surogates.storage.kb_store import KbStore

    storage = LocalBackend(base_path=str(tmp_path / "garage"))
    org_id = await create_org(session_factory)
    agent_id = await _seed_agent(session_factory, org_id)

    plat_kb = uuid.uuid4()
    plat_source = uuid.uuid4()
    docs = tmp_path / "platdocs"
    docs.mkdir()
    (docs / "shared.md").write_text("# Shared\n\nplatform-only-token rare-platform-word\n")
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO kb (id, org_id, name, agents_md, is_platform) "
                "VALUES (:id, NULL, :name, '', true)"
            ),
            {"id": plat_kb, "name": f"plat-{uuid.uuid4()}"},
        )
        await db.execute(
            text(
                "INSERT INTO kb_source (id, kb_id, kind, config) "
                "VALUES (:id, :kb_id, 'markdown_dir', :config)"
            ),
            {
                "id": plat_source,
                "kb_id": plat_kb,
                "config": json.dumps({"path": str(docs)}),
            },
        )
        await db.commit()
    await run_ingest(
        plat_source,
        session_factory=session_factory,
        storage_backend=storage,
    )
    await compile_wiki_for_kb(
        plat_kb,
        session_factory=session_factory,
        storage_backend=storage,
        embedder=None,
    )

    store = KbStore(session_factory)
    hits = await store.search(
        org_id=org_id,
        query="rare-platform-word",
        agent_id=agent_id,
    )
    assert hits, "platform KB should be visible without an explicit grant"
