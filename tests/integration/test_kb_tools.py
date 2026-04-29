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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from surogates.tools.builtin import kb_read as kb_read_mod
from surogates.tools.builtin import kb_search as kb_search_mod
from surogates.tools.registry import ToolRegistry

from .conftest import create_org

pytestmark = pytest.mark.asyncio(loop_scope="session")


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


async def test_kb_read_resolves_own_org_wiki_entry(session_factory):
    """Happy path: own org, registered wiki entry → success with the
    step-3 stub content note. Validates kwargs flow + DB resolution.
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
    # Stub note for step 3; content fetch lands in step 4.
    assert "step 4" in payload["content"].lower()


async def test_kb_read_resolves_platform_kb_entry(session_factory):
    """Platform KBs are visible to every org → kb_read from any org
    can resolve a platform wiki entry.
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


async def test_kb_read_combined_path_form(session_factory):
    """``path`` as ``<kb_name>/<rest>`` (no separate ``kb`` arg) also
    works.
    """
    org_id = await create_org(session_factory)
    kb_name = f"own-{uuid.uuid4()}"
    kb_id = await _seed_kb(session_factory, org_id, name=kb_name)
    await _seed_wiki_entry(
        session_factory, kb_id, path="wiki/concepts/sandbox.md"
    )
    registry = _make_registry()
    raw = await registry.dispatch(
        "kb_read",
        {"path": f"{kb_name}/wiki/concepts/sandbox.md"},
        session_factory=session_factory,
        tenant={"org_id": org_id},
    )
    payload = json.loads(raw)
    assert payload["success"] is True
    assert payload["kb_name"] == kb_name
    assert payload["path"] == "wiki/concepts/sandbox.md"
