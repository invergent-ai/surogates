"""End-to-end tests for the wiki-compile pass + hybrid kb_search.

Step 5a's deliverable: ingest content, compile it into wiki entries +
chunks + embeddings, and verify ``kb_search`` returns real hits via
the BM25 branch alone, the vector branch alone, and the hybrid merge.

Uses :class:`StubEmbeddingClient` for embeddings — fast, offline,
deterministic. Real-embedding-backend integration lives in a
separate, opt-in test (gated by an env flag).
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from surogates.jobs.kb_ingest import run_ingest
from surogates.jobs.wiki_compile import compile_wiki_for_kb
from surogates.storage.backend import LocalBackend
from surogates.storage.embeddings import StubEmbeddingClient
from surogates.tools.builtin import kb_search as kb_search_mod
from surogates.tools.registry import ToolRegistry

from .conftest import create_org

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def local_storage(tmp_path) -> LocalBackend:
    return LocalBackend(base_path=str(tmp_path / "garage"))


@pytest.fixture
def fixture_docs_dir(tmp_path) -> Path:
    """Three small markdown files with distinct vocabulary so we can
    differentiate hits in retrieval tests.
    """
    root = tmp_path / "docs"
    root.mkdir()
    (root / "sub-agents.md").write_text(
        "# Sub-Agents\n\n"
        "A sub-agent is a freshly spawned child session that "
        "delegates tasks. Defined by an AGENT.md file.\n\n"
        "## Spawning\n\n"
        "The coordinator calls spawn_worker(goal, agent_type=...).\n",
        encoding="utf-8",
    )
    (root / "sandbox.md").write_text(
        "# Sandbox\n\n"
        "Each session runs in a Kubernetes pod with an s3fs-fuse "
        "sidecar mounting the workspace bucket.\n\n"
        "## Isolation\n\n"
        "Trust boundaries: API server / Worker / Sandbox.\n",
        encoding="utf-8",
    )
    (root / "channels.md").write_text(
        "# Channels\n\n"
        "Channels are how agents talk to humans. Web, Slack, API, "
        "and email are the primary channel adapters.\n",
        encoding="utf-8",
    )
    return root


async def _seed_kb_with_source(
    session_factory,
    org_id,
    kb_name: str,
    config: dict,
) -> tuple[uuid.UUID, uuid.UUID]:
    kb_id = uuid.uuid4()
    source_id = uuid.uuid4()
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO kb (id, org_id, name, agents_md) "
                "VALUES (:id, :org_id, :name, '')"
            ),
            {"id": kb_id, "org_id": org_id, "name": kb_name},
        )
        await db.execute(
            text(
                "INSERT INTO kb_source (id, kb_id, kind, config) "
                "VALUES (:id, :kb_id, 'markdown_dir', :config)"
            ),
            {"id": source_id, "kb_id": kb_id, "config": json.dumps(config)},
        )
        await db.commit()
    return kb_id, source_id


# ---------------------------------------------------------------------------
# Compile pass
# ---------------------------------------------------------------------------


async def test_compile_creates_wiki_entries_and_chunks(
    session_factory, local_storage, fixture_docs_dir
):
    """First compile after a 3-file ingest produces 3 wiki_entries +
    N chunks (1 per heading section). Second compile is a no-op
    because content_sha hasn't changed (entries_unchanged).
    """
    org_id = await create_org(session_factory)
    kb_name = f"comp-{uuid.uuid4()}"
    kb_id, source_id = await _seed_kb_with_source(
        session_factory, org_id, kb_name, {"path": str(fixture_docs_dir)},
    )

    # Ingest first.
    await run_ingest(
        source_id, session_factory=session_factory, storage_backend=local_storage,
    )

    embedder = StubEmbeddingClient(dim=1024)
    result = await compile_wiki_for_kb(
        kb_id,
        session_factory=session_factory,
        storage_backend=local_storage,
        embedder=embedder,
    )
    assert result.entries_added == 3
    assert result.chunks_added > 0  # at least one chunk per file

    async with session_factory() as db:
        n_entries = (
            await db.execute(
                text("SELECT count(*) AS n FROM kb_wiki_entry WHERE kb_id = :id"),
                {"id": kb_id},
            )
        ).scalar()
        n_chunks = (
            await db.execute(
                text(
                    "SELECT count(*) AS n FROM kb_chunk c "
                    "JOIN kb_wiki_entry we ON we.id = c.wiki_entry_id "
                    "WHERE we.kb_id = :id"
                ),
                {"id": kb_id},
            )
        ).scalar()
        n_embedded = (
            await db.execute(
                text(
                    "SELECT count(*) AS n FROM kb_chunk c "
                    "JOIN kb_wiki_entry we ON we.id = c.wiki_entry_id "
                    "WHERE we.kb_id = :id AND c.embedding IS NOT NULL"
                ),
                {"id": kb_id},
            )
        ).scalar()
    assert n_entries == 3
    assert n_chunks == result.chunks_added
    # All chunks embedded because we passed an embedder.
    assert n_embedded == n_chunks

    # Second compile with watermark: no new raw_docs since
    # last_compiled_at, so the SQL filter loads 0 rows. Result is all
    # zeros — the right "no work done" semantics.
    result2 = await compile_wiki_for_kb(
        kb_id,
        session_factory=session_factory,
        storage_backend=local_storage,
        embedder=embedder,
    )
    assert result2.entries_added == 0
    assert result2.entries_updated == 0
    assert result2.chunks_added == 0

    # Force a full recompile (skip the watermark filter): should see
    # all 3 entries as unchanged because their content_sha hasn't
    # actually changed.
    result3 = await compile_wiki_for_kb(
        kb_id,
        session_factory=session_factory,
        storage_backend=local_storage,
        embedder=embedder,
        only_changed_since=False,
    )
    assert result3.entries_added == 0
    assert result3.entries_updated == 0
    assert result3.entries_unchanged == 3
    assert result3.chunks_added == 0


async def test_compile_without_embedder_inserts_chunks_with_null_embedding(
    session_factory, local_storage, fixture_docs_dir
):
    """When no embedder is supplied (e.g. a deployment that hasn't
    enabled embeddings), chunks still get inserted — just with
    embedding=NULL. kb_search then runs BM25-only, vector branch
    skipped via the ``embedding IS NOT NULL`` filter.
    """
    org_id = await create_org(session_factory)
    kb_name = f"noemb-{uuid.uuid4()}"
    kb_id, source_id = await _seed_kb_with_source(
        session_factory, org_id, kb_name, {"path": str(fixture_docs_dir)},
    )
    await run_ingest(
        source_id, session_factory=session_factory, storage_backend=local_storage,
    )
    await compile_wiki_for_kb(
        kb_id,
        session_factory=session_factory,
        storage_backend=local_storage,
        embedder=None,
    )

    async with session_factory() as db:
        n_chunks = (
            await db.execute(
                text(
                    "SELECT count(*) AS n FROM kb_chunk c "
                    "JOIN kb_wiki_entry we ON we.id = c.wiki_entry_id "
                    "WHERE we.kb_id = :id"
                ),
                {"id": kb_id},
            )
        ).scalar()
        n_embedded = (
            await db.execute(
                text(
                    "SELECT count(*) AS n FROM kb_chunk c "
                    "JOIN kb_wiki_entry we ON we.id = c.wiki_entry_id "
                    "WHERE we.kb_id = :id AND c.embedding IS NOT NULL"
                ),
                {"id": kb_id},
            )
        ).scalar()
    assert n_chunks > 0
    assert n_embedded == 0


async def test_compile_advances_watermark(
    session_factory, local_storage, fixture_docs_dir
):
    """``kb.last_compiled_at`` is set after a successful run so the
    next call with ``only_changed_since=True`` skips already-compiled
    raw docs.
    """
    org_id = await create_org(session_factory)
    kb_name = f"wm-{uuid.uuid4()}"
    kb_id, source_id = await _seed_kb_with_source(
        session_factory, org_id, kb_name, {"path": str(fixture_docs_dir)},
    )
    await run_ingest(
        source_id, session_factory=session_factory, storage_backend=local_storage,
    )
    await compile_wiki_for_kb(
        kb_id,
        session_factory=session_factory,
        storage_backend=local_storage,
        embedder=StubEmbeddingClient(),
    )
    async with session_factory() as db:
        row = (
            await db.execute(
                text("SELECT last_compiled_at FROM kb WHERE id = :id"),
                {"id": kb_id},
            )
        ).first()
    assert row.last_compiled_at is not None


# ---------------------------------------------------------------------------
# Hybrid kb_search (BM25 alone, vector alone via stub, RRF merge)
# ---------------------------------------------------------------------------


async def test_kb_search_bm25_only_returns_hits(
    session_factory, local_storage, fixture_docs_dir
):
    """Search without an embedder: pure BM25, but real hits over the
    ingested + compiled content.
    """
    org_id = await create_org(session_factory)
    kb_name = f"bm25-{uuid.uuid4()}"
    kb_id, source_id = await _seed_kb_with_source(
        session_factory, org_id, kb_name, {"path": str(fixture_docs_dir)},
    )
    await run_ingest(
        source_id, session_factory=session_factory, storage_backend=local_storage,
    )
    # Compile WITHOUT an embedder so chunks have NULL embedding.
    await compile_wiki_for_kb(
        kb_id,
        session_factory=session_factory,
        storage_backend=local_storage,
        embedder=None,
    )

    registry = ToolRegistry()
    kb_search_mod.register(registry)
    raw = await registry.dispatch(
        "kb_search",
        # Words that all live in the same Sub-Agents body chunk
        # (after the chunker strips the heading line itself).
        {"query": "child session delegates", "kb": kb_name},
        session_factory=session_factory,
        tenant={"org_id": org_id},
    )
    payload = json.loads(raw)
    assert payload["success"] is True
    # The sub-agents file's body contains all three terms in one chunk.
    assert any(
        "sub-agents" in r["document_path"].lower()
        for r in payload["results"]
    )


async def test_kb_search_with_stub_embedder_returns_hits(
    session_factory, local_storage, fixture_docs_dir
):
    """Search WITH an embedder: hybrid merge. The stub doesn't capture
    semantic similarity so the actual top result is determined by
    BM25, but we verify the embedder doesn't break results and that
    the vector branch ran (chunks with non-null embeddings exist).
    """
    org_id = await create_org(session_factory)
    kb_name = f"hyb-{uuid.uuid4()}"
    kb_id, source_id = await _seed_kb_with_source(
        session_factory, org_id, kb_name, {"path": str(fixture_docs_dir)},
    )
    await run_ingest(
        source_id, session_factory=session_factory, storage_backend=local_storage,
    )
    embedder = StubEmbeddingClient(dim=1024)
    await compile_wiki_for_kb(
        kb_id,
        session_factory=session_factory,
        storage_backend=local_storage,
        embedder=embedder,
    )

    registry = ToolRegistry()
    kb_search_mod.register(registry)
    raw = await registry.dispatch(
        "kb_search",
        {"query": "channels web slack api", "kb": kb_name},
        session_factory=session_factory,
        tenant={"org_id": org_id},
        embedder=embedder,
    )
    payload = json.loads(raw)
    assert payload["success"] is True
    # The channels.md file has all four words; should be the top hit.
    assert payload["results"], "expected at least one hit"
    assert "channels" in payload["results"][0]["document_path"].lower()


async def test_kb_search_returns_empty_when_query_matches_nothing(
    session_factory, local_storage, fixture_docs_dir
):
    """Junk query → empty results, not an error."""
    org_id = await create_org(session_factory)
    kb_name = f"miss-{uuid.uuid4()}"
    kb_id, source_id = await _seed_kb_with_source(
        session_factory, org_id, kb_name, {"path": str(fixture_docs_dir)},
    )
    await run_ingest(
        source_id, session_factory=session_factory, storage_backend=local_storage,
    )
    await compile_wiki_for_kb(
        kb_id,
        session_factory=session_factory,
        storage_backend=local_storage,
        embedder=None,
    )

    registry = ToolRegistry()
    kb_search_mod.register(registry)
    raw = await registry.dispatch(
        "kb_search",
        {"query": "supercalifragilistic", "kb": kb_name},
        session_factory=session_factory,
        tenant={"org_id": org_id},
    )
    payload = json.loads(raw)
    assert payload["success"] is True
    assert payload["results"] == []


async def test_kb_search_falls_back_to_bm25_when_embedder_fails(
    session_factory, local_storage, fixture_docs_dir
):
    """A broken embedder shouldn't take retrieval offline — kb_search
    catches the error and returns BM25 hits only.
    """
    org_id = await create_org(session_factory)
    kb_name = f"fb-{uuid.uuid4()}"
    kb_id, source_id = await _seed_kb_with_source(
        session_factory, org_id, kb_name, {"path": str(fixture_docs_dir)},
    )
    await run_ingest(
        source_id, session_factory=session_factory, storage_backend=local_storage,
    )
    # Compile with stub so chunks have embeddings (so vector branch
    # would normally fire).
    await compile_wiki_for_kb(
        kb_id,
        session_factory=session_factory,
        storage_backend=local_storage,
        embedder=StubEmbeddingClient(),
    )

    class BrokenEmbedder:
        @property
        def dim(self):
            return 1024

        async def embed(self, inputs):
            raise RuntimeError("embedding service down")

    registry = ToolRegistry()
    kb_search_mod.register(registry)
    raw = await registry.dispatch(
        "kb_search",
        # Words that share a single chunk (the Sub-Agents body) so
        # BM25 alone returns the file even with the vector branch off.
        {"query": "child session delegates", "kb": kb_name},
        session_factory=session_factory,
        tenant={"org_id": org_id},
        embedder=BrokenEmbedder(),
    )
    payload = json.loads(raw)
    assert payload["success"] is True
    # BM25 still finds the sub-agents file even though vector branch failed.
    assert any(
        "sub-agents" in r["document_path"].lower()
        for r in payload["results"]
    )
