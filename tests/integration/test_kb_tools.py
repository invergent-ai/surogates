"""kb_search and kb_read HARNESS tool tests — empty-wiki plumbing.

Step-3 scope: validate that

  - both tools register with the right schemas in the runtime registry
  - dispatch through ``ToolRegistry.dispatch`` works
  - kwargs injection (``session_factory``, ``tenant``) flows correctly
  - tenant scoping is enforced (no cross-org reads)
  - empty corpora return clean empty results, not errors
  - missing GUC / org_id surfaces a clean failure
  - federated vs single-KB scoping shapes work

Byte content fetch from object storage is not yet wired (lands in step
4), so kb_read tests cover registration + tenant scoping only — the
``content`` field returned for a found entry is a stub note.
"""
from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from surogates.storage.backend import LocalBackend
from surogates.storage.kb_storage import KbStorage
from surogates.tools.builtin import kb_read as kb_read_mod
from surogates.tools.builtin import kb_search as kb_search_mod
from surogates.tools.registry import ToolRegistry

from .conftest import create_org

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# LocalBackend fixture for storage-backed kb_read tests
# ---------------------------------------------------------------------------


@pytest.fixture
def local_storage(tmp_path) -> LocalBackend:
    """LocalBackend rooted at the test's tmp dir.

    Per-test scope so tests don't see each other's seeded bytes.
    """
    return LocalBackend(base_path=str(tmp_path))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry() -> ToolRegistry:
    """Fresh registry with both KB tools registered."""
    registry = ToolRegistry()
    kb_search_mod.register(registry)
    kb_read_mod.register(registry)
    return registry


async def _seed_kb(
    session_factory: async_sessionmaker,
    org_id: uuid.UUID | None,
    name: str | None = None,
) -> uuid.UUID:
    kb_id = uuid.uuid4()
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO kb (id, org_id, name, agents_md, is_platform) "
                "VALUES (:id, :org_id, :name, '', :is_platform)"
            ),
            {
                "id": kb_id,
                "org_id": org_id,
                "name": name or f"kb-{kb_id}",
                "is_platform": org_id is None,
            },
        )
        await db.commit()
    return kb_id


async def _seed_wiki_entry(
    session_factory: async_sessionmaker,
    kb_id: uuid.UUID,
    path: str = "wiki/summaries/test.md",
) -> uuid.UUID:
    entry_id = uuid.uuid4()
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO kb_wiki_entry "
                "(id, kb_id, path, kind, content_sha) "
                "VALUES (:id, :kb_id, :path, 'summary', 'sha')"
            ),
            {"id": entry_id, "kb_id": kb_id, "path": path},
        )
        await db.commit()
    return entry_id


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_both_tools_register():
    registry = _make_registry()
    names = registry.tool_names
    assert "kb_search" in names
    assert "kb_read" in names


async def test_kb_search_schema_shape():
    registry = _make_registry()
    entry = registry.get("kb_search")
    assert entry is not None
    schema = entry.schema
    assert schema.name == "kb_search"
    params = schema.parameters
    assert params["type"] == "object"
    props = params["properties"]
    assert set(props) >= {"query", "kb", "top_k"}
    assert params["required"] == ["query"]


async def test_kb_read_schema_shape():
    registry = _make_registry()
    entry = registry.get("kb_read")
    assert entry is not None
    params = entry.schema.parameters
    assert set(params["properties"]) >= {"path", "kb"}
    assert params["required"] == ["path"]


# ---------------------------------------------------------------------------
# Missing kwargs / tenant context
# ---------------------------------------------------------------------------


async def test_kb_search_without_session_factory_returns_error():
    registry = _make_registry()
    raw = await registry.dispatch("kb_search", {"query": "hello"})
    payload = json.loads(raw)
    assert payload["success"] is False
    assert "session_factory" in payload["error"]


async def test_kb_search_without_tenant_returns_error(session_factory):
    registry = _make_registry()
    raw = await registry.dispatch(
        "kb_search",
        {"query": "hello"},
        session_factory=session_factory,
    )
    payload = json.loads(raw)
    assert payload["success"] is False
    assert "org_id" in payload["error"]


async def test_kb_read_without_tenant_returns_error(session_factory):
    registry = _make_registry()
    raw = await registry.dispatch(
        "kb_read",
        {"path": "x/y.md"},
        session_factory=session_factory,
    )
    payload = json.loads(raw)
    assert payload["success"] is False
    assert "org_id" in payload["error"]


# ---------------------------------------------------------------------------
# kb_search against an empty corpus
# ---------------------------------------------------------------------------


async def test_kb_search_empty_query_returns_empty_results(session_factory):
    org_id = await create_org(session_factory)
    registry = _make_registry()
    raw = await registry.dispatch(
        "kb_search",
        {"query": "   "},
        session_factory=session_factory,
        tenant={"org_id": org_id},
    )
    payload = json.loads(raw)
    assert payload == {"success": True, "results": []}


async def test_kb_search_empty_corpus_returns_empty_results(session_factory):
    """Org has no KB rows at all → kb_search returns []."""
    org_id = await create_org(session_factory)
    registry = _make_registry()
    raw = await registry.dispatch(
        "kb_search",
        {"query": "anything"},
        session_factory=session_factory,
        tenant={"org_id": org_id},
    )
    payload = json.loads(raw)
    assert payload == {"success": True, "results": []}


async def test_kb_search_unknown_kb_name_returns_empty_results(session_factory):
    org_id = await create_org(session_factory)
    registry = _make_registry()
    raw = await registry.dispatch(
        "kb_search",
        {"query": "hello", "kb": "no-such-kb"},
        session_factory=session_factory,
        tenant={"org_id": org_id},
    )
    payload = json.loads(raw)
    assert payload == {"success": True, "results": []}


async def test_kb_search_kb_with_no_chunks_returns_empty_results(session_factory):
    """KB exists but no chunks ingested → still returns []."""
    org_id = await create_org(session_factory)
    kb_name = f"empty-{uuid.uuid4()}"
    await _seed_kb(session_factory, org_id, name=kb_name)
    registry = _make_registry()
    raw = await registry.dispatch(
        "kb_search",
        {"query": "anything", "kb": kb_name},
        session_factory=session_factory,
        tenant={"org_id": org_id},
    )
    payload = json.loads(raw)
    assert payload == {"success": True, "results": []}


# ---------------------------------------------------------------------------
# kb_search tenant scoping
# ---------------------------------------------------------------------------


async def test_kb_search_does_not_leak_other_orgs_kb_by_name(session_factory):
    """Org A asks for org B's KB by name → result is empty. The search
    cannot reach any KB the org cannot see, even when given the name.
    """
    org_a = await create_org(session_factory)
    org_b = await create_org(session_factory)
    b_name = f"orgB-{uuid.uuid4()}"
    await _seed_kb(session_factory, org_b, name=b_name)
    registry = _make_registry()
    raw = await registry.dispatch(
        "kb_search",
        {"query": "anything", "kb": b_name},
        session_factory=session_factory,
        tenant={"org_id": org_a},
    )
    payload = json.loads(raw)
    assert payload == {"success": True, "results": []}


# ---------------------------------------------------------------------------
# kb_read
# ---------------------------------------------------------------------------


async def test_kb_read_unknown_kb_returns_not_found(session_factory):
    org_id = await create_org(session_factory)
    registry = _make_registry()
    raw = await registry.dispatch(
        "kb_read",
        {"path": "wiki/summaries/foo.md", "kb": "no-such-kb"},
        session_factory=session_factory,
        tenant={"org_id": org_id},
    )
    payload = json.loads(raw)
    assert payload["success"] is False
    assert "not found" in payload["error"].lower()


async def test_kb_read_unregistered_path_returns_not_found(session_factory):
    """KB exists but the path isn't registered as wiki or raw → not found."""
    org_id = await create_org(session_factory)
    kb_name = f"empty-{uuid.uuid4()}"
    await _seed_kb(session_factory, org_id, name=kb_name)
    registry = _make_registry()
    raw = await registry.dispatch(
        "kb_read",
        {"path": "wiki/summaries/missing.md", "kb": kb_name},
        session_factory=session_factory,
        tenant={"org_id": org_id},
    )
    payload = json.loads(raw)
    assert payload["success"] is False
    assert "not found" in payload["error"].lower()


async def test_kb_read_cross_tenant_returns_not_found(session_factory):
    """Org A reads org B's wiki entry → rejected as not-found."""
    org_a = await create_org(session_factory)
    org_b = await create_org(session_factory)
    b_kb_name = f"orgB-{uuid.uuid4()}"
    b_kb = await _seed_kb(session_factory, org_b, name=b_kb_name)
    await _seed_wiki_entry(session_factory, b_kb, path="wiki/summaries/x.md")
    registry = _make_registry()
    raw = await registry.dispatch(
        "kb_read",
        {"path": "wiki/summaries/x.md", "kb": b_kb_name},
        session_factory=session_factory,
        tenant={"org_id": org_a},
    )
    payload = json.loads(raw)
    assert payload["success"] is False
    assert "not found" in payload["error"].lower()


async def test_kb_read_resolves_own_org_wiki_entry_without_storage(session_factory):
    """No storage_backend in kwargs → registration is confirmed but
    content is the "backend not wired" stub. Verifies kwargs flow +
    DB resolution work even when storage isn't available (used by
    tests that don't set up a backend).
    """
    org_id = await create_org(session_factory)
    kb_name = f"own-{uuid.uuid4()}"
    kb_id = await _seed_kb(session_factory, org_id, name=kb_name)
    await _seed_wiki_entry(
        session_factory, kb_id, path="wiki/summaries/foo.md"
    )
    registry = _make_registry()
    raw = await registry.dispatch(
        "kb_read",
        {"path": "wiki/summaries/foo.md", "kb": kb_name},
        session_factory=session_factory,
        tenant={"org_id": org_id},
    )
    payload = json.loads(raw)
    assert payload["success"] is True
    assert payload["kb_name"] == kb_name
    assert payload["path"] == "wiki/summaries/foo.md"
    assert payload["kind"] == "wiki"
    assert "storage backend not wired" in payload["content"].lower()


async def test_kb_read_returns_real_bytes_when_storage_wired(
    session_factory, local_storage
):
    """Happy path: storage_backend in kwargs + bytes seeded at the right
    bucket key → kb_read returns the real content.
    """
    org_id = await create_org(session_factory)
    kb_name = f"own-{uuid.uuid4()}"
    kb_id = await _seed_kb(session_factory, org_id, name=kb_name)
    path = "wiki/summaries/sub-agents.md"
    await _seed_wiki_entry(session_factory, kb_id, path=path)

    # Seed bytes via the same KbStorage helper the production path uses.
    helper = KbStorage(local_storage)
    await helper.write_entry(
        kb_org_id=org_id,
        kb_name=kb_name,
        path=path,
        data=b"# Sub-Agents\n\nContent here.",
    )

    registry = _make_registry()
    raw = await registry.dispatch(
        "kb_read",
        {"path": path, "kb": kb_name},
        session_factory=session_factory,
        tenant={"org_id": org_id},
        storage_backend=local_storage,
    )
    payload = json.loads(raw)
    assert payload["success"] is True
    assert payload["content"] == "# Sub-Agents\n\nContent here."


async def test_kb_read_partial_ingest_note_when_db_row_exists_but_bytes_missing(
    session_factory, local_storage
):
    """Storage backend wired but the object isn't there (e.g. a previous
    ingest crashed after inserting the DB row). kb_read returns success
    with an explanatory partial-ingest note rather than crashing.
    """
    org_id = await create_org(session_factory)
    kb_name = f"own-{uuid.uuid4()}"
    kb_id = await _seed_kb(session_factory, org_id, name=kb_name)
    path = "wiki/summaries/orphan.md"
    await _seed_wiki_entry(session_factory, kb_id, path=path)
    # No write_entry call — bucket object intentionally missing.

    registry = _make_registry()
    raw = await registry.dispatch(
        "kb_read",
        {"path": path, "kb": kb_name},
        session_factory=session_factory,
        tenant={"org_id": org_id},
        storage_backend=local_storage,
    )
    payload = json.loads(raw)
    assert payload["success"] is True
    assert "partial ingest" in payload["content"].lower()


async def test_kb_read_routes_platform_to_platform_bucket(
    session_factory, local_storage
):
    """Platform KBs (org_id IS NULL) live in the platform-shared bucket,
    NOT in any tenant bucket. Verifies kb_read uses kb.org_id from the
    DB row (not the caller's org_id) for bucket routing.
    """
    plat_name = f"platform-{uuid.uuid4()}"
    plat_kb = await _seed_kb(session_factory, None, name=plat_name)
    path = "index.md"
    await _seed_wiki_entry(session_factory, plat_kb, path=path)

    helper = KbStorage(local_storage)
    await helper.write_entry(
        kb_org_id=None,  # platform
        kb_name=plat_name,
        path=path,
        data=b"# Platform index",
    )
    # Sanity: writing with the wrong (tenant) org_id would put it in a
    # different bucket; verify by attempting a read with the wrong arg.
    wrong_org = await create_org(session_factory)
    assert (
        await helper.read_entry(wrong_org, plat_name, path)
    ) is None, "platform bytes should NOT live in any tenant bucket"

    # Now exercise the tool from a regular org's perspective; it should
    # see the platform content.
    org_id = await create_org(session_factory)
    registry = _make_registry()
    raw = await registry.dispatch(
        "kb_read",
        {"path": path, "kb": plat_name},
        session_factory=session_factory,
        tenant={"org_id": org_id},
        storage_backend=local_storage,
    )
    payload = json.loads(raw)
    assert payload["success"] is True
    assert payload["content"] == "# Platform index"


async def test_kb_read_combined_path_form_with_storage(
    session_factory, local_storage
):
    """``path`` as ``<kb_name>/<rest>`` (no separate ``kb`` arg) with a
    real storage backend wired — content comes back as bytes.
    """
    org_id = await create_org(session_factory)
    kb_name = f"own-{uuid.uuid4()}"
    kb_id = await _seed_kb(session_factory, org_id, name=kb_name)
    path = "wiki/concepts/sandbox.md"
    await _seed_wiki_entry(session_factory, kb_id, path=path)
    helper = KbStorage(local_storage)
    await helper.write_entry(
        kb_org_id=org_id, kb_name=kb_name, path=path, data=b"# Sandbox",
    )

    registry = _make_registry()
    raw = await registry.dispatch(
        "kb_read",
        {"path": f"{kb_name}/{path}"},
        session_factory=session_factory,
        tenant={"org_id": org_id},
        storage_backend=local_storage,
    )
    payload = json.loads(raw)
    assert payload["success"] is True
    assert payload["content"] == "# Sandbox"


async def test_kb_read_resolves_platform_kb_entry_without_storage(session_factory):
    """Platform KBs are visible to every org → kb_read from any org
    resolves the registration even without a storage backend (content
    is the no-backend stub).
    """
    plat_name = f"platform-{uuid.uuid4()}"
    plat_kb = await _seed_kb(session_factory, None, name=plat_name)
    await _seed_wiki_entry(
        session_factory, plat_kb, path="index.md"
    )
    org_id = await create_org(session_factory)
    registry = _make_registry()
    raw = await registry.dispatch(
        "kb_read",
        {"path": "index.md", "kb": plat_name},
        session_factory=session_factory,
        tenant={"org_id": org_id},
    )
    payload = json.loads(raw)
    assert payload["success"] is True
    assert payload["kb_name"] == plat_name
