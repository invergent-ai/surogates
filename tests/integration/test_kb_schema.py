"""KB schema verification — pgvector, tsvector GENERATED, GIN + HNSW
indexes, CASCADE deletes, unique constraints, idempotent DDL re-run.

These tests cover the non-tenant-isolation aspects of the KB schema set
up by ``surogates/db/kb.sql``. Row-level-security tests live in
``test_kb_isolation.py`` (MVP step 2) because they require setting up a
non-superuser role to exercise — testcontainers connects as a superuser
which always bypasses RLS regardless of FORCE.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from .conftest import create_org

# Bind every test to the session-scoped event loop so they share the same
# loop as the session-scoped ``engine`` / ``session_factory`` fixtures.
# Without this we get ``Future attached to a different loop``.
pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_kb(
    session_factory: async_sessionmaker,
    org_id: uuid.UUID | None = None,
    name: str | None = None,
) -> uuid.UUID:
    """Insert a KB row (org-owned by default; pass ``org_id=None`` for
    a platform KB) and return its id.
    """
    kb_id = uuid.uuid4()
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO kb (id, org_id, name, agents_md, is_platform) "
                "VALUES (:id, :org_id, :name, :agents_md, :is_platform)"
            ),
            {
                "id": kb_id,
                "org_id": org_id,
                "name": name or f"kb-{kb_id}",
                "agents_md": "",
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
                "VALUES (:id, :kb_id, :path, :kind, :sha)"
            ),
            {
                "id": entry_id,
                "kb_id": kb_id,
                "path": path,
                "kind": "summary",
                "sha": "deadbeef",
            },
        )
        await db.commit()
    return entry_id


# ---------------------------------------------------------------------------
# Schema sanity
# ---------------------------------------------------------------------------


async def test_pgvector_extension_installed(session_factory):
    async with session_factory() as db:
        row = (
            await db.execute(
                text("SELECT extname, extversion FROM pg_extension WHERE extname='vector'")
            )
        ).first()
    assert row is not None, "pgvector extension not installed"
    assert row.extname == "vector"


async def test_kb_tables_exist(session_factory):
    expected = {
        "kb",
        "kb_source",
        "kb_raw_doc",
        "kb_wiki_entry",
        "kb_chunk",
        "agent_kb_grant",
    }
    async with session_factory() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname='public' AND tablename = ANY(:names)"
                ),
                {"names": list(expected)},
            )
        ).all()
    assert {r.tablename for r in rows} == expected


async def test_kb_chunk_has_tsv_generated_and_embedding_columns(session_factory):
    async with session_factory() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT column_name, data_type, is_generated "
                    "FROM information_schema.columns "
                    "WHERE table_name='kb_chunk' "
                    "AND column_name IN ('tsv', 'embedding')"
                )
            )
        ).all()
    cols = {r.column_name: r for r in rows}
    assert "tsv" in cols, "tsv column missing on kb_chunk"
    assert cols["tsv"].data_type == "tsvector"
    assert cols["tsv"].is_generated == "ALWAYS"
    assert "embedding" in cols, "embedding column missing on kb_chunk"
    # pgvector reports the type as "USER-DEFINED" via information_schema
    assert cols["embedding"].data_type in ("USER-DEFINED", "vector")


async def test_kb_chunk_indexes_present(session_factory):
    async with session_factory() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE tablename='kb_chunk'"
                )
            )
        ).all()
    by_name = {r.indexname: r.indexdef for r in rows}
    assert "idx_kb_chunk_tsv" in by_name
    assert "gin" in by_name["idx_kb_chunk_tsv"].lower()
    assert "idx_kb_chunk_embedding" in by_name
    assert "hnsw" in by_name["idx_kb_chunk_embedding"].lower()
    assert "vector_cosine_ops" in by_name["idx_kb_chunk_embedding"]


async def test_rls_enabled_and_forced_on_all_kb_tables(session_factory):
    expected = {
        "kb",
        "kb_source",
        "kb_raw_doc",
        "kb_wiki_entry",
        "kb_chunk",
        "agent_kb_grant",
    }
    async with session_factory() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity "
                    "FROM pg_class "
                    "WHERE relname = ANY(:names) AND relkind = 'r'"
                ),
                {"names": list(expected)},
            )
        ).all()
    state = {r.relname: (r.relrowsecurity, r.relforcerowsecurity) for r in rows}
    for name in expected:
        assert name in state, f"{name} not found"
        rls_on, force_on = state[name]
        assert rls_on, f"RLS not enabled on {name}"
        assert force_on, f"FORCE RLS not set on {name}"


async def test_tenant_iso_policy_present_on_each_kb_table(session_factory):
    expected_policies = {
        "kb": "kb_tenant_iso",
        "kb_source": "kb_source_tenant_iso",
        "kb_raw_doc": "kb_raw_doc_tenant_iso",
        "kb_wiki_entry": "kb_wiki_entry_tenant_iso",
        "kb_chunk": "kb_chunk_tenant_iso",
        "agent_kb_grant": "agent_kb_grant_tenant_iso",
    }
    async with session_factory() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT tablename, policyname FROM pg_policies "
                    "WHERE tablename = ANY(:names)"
                ),
                {"names": list(expected_policies)},
            )
        ).all()
    found = {(r.tablename, r.policyname) for r in rows}
    for table, policy in expected_policies.items():
        assert (table, policy) in found, f"missing policy {policy} on {table}"


# ---------------------------------------------------------------------------
# Functional: vector + tsvector + indexes serve queries
# ---------------------------------------------------------------------------


async def test_kb_chunk_accepts_and_returns_pgvector_value(session_factory):
    """Insert a 1024-dim embedding and read it back. Validates that the
    pgvector column type works end-to-end through asyncpg+SQLAlchemy.
    """
    org_id = await create_org(session_factory)
    kb_id = await _seed_kb(session_factory, org_id=org_id)
    entry_id = await _seed_wiki_entry(session_factory, kb_id)
    vec = [0.0] * 1024
    vec[7] = 1.0
    chunk_id = uuid.uuid4()
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO kb_chunk "
                "(id, wiki_entry_id, chunk_index, content, embedding) "
                "VALUES (:id, :entry_id, :idx, :content, :emb)"
            ),
            {
                "id": chunk_id,
                "entry_id": entry_id,
                "idx": 0,
                "content": "hello world",
                "emb": str(vec),  # pgvector accepts the literal string form
            },
        )
        await db.commit()
        # Read back, confirm dimensionality.
        row = (
            await db.execute(
                text(
                    "SELECT vector_dims(embedding) AS dims "
                    "FROM kb_chunk WHERE id=:id"
                ),
                {"id": chunk_id},
            )
        ).first()
    assert row.dims == 1024


async def test_kb_chunk_tsv_populated_on_insert(session_factory):
    """The GENERATED tsv column should be populated automatically from
    ``content`` on insert -- callers never set it explicitly.
    """
    org_id = await create_org(session_factory)
    kb_id = await _seed_kb(session_factory, org_id=org_id)
    entry_id = await _seed_wiki_entry(session_factory, kb_id)
    chunk_id = uuid.uuid4()
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO kb_chunk (id, wiki_entry_id, chunk_index, content) "
                "VALUES (:id, :entry_id, 0, :content)"
            ),
            {
                "id": chunk_id,
                "entry_id": entry_id,
                "content": "ripgrep finds patterns in files quickly",
            },
        )
        await db.commit()
        row = (
            await db.execute(
                text("SELECT tsv::text AS tsv_text FROM kb_chunk WHERE id=:id"),
                {"id": chunk_id},
            )
        ).first()
    assert row.tsv_text is not None
    # Stemmed lexemes — "patterns" -> "pattern", "files" -> "file", etc.
    for lex in ("ripgrep", "pattern", "file", "quick"):
        assert lex in row.tsv_text, f"expected {lex!r} in tsvector {row.tsv_text!r}"


async def test_kb_chunk_tsv_gin_query_returns_matching_rows(session_factory):
    """``WHERE tsv @@ plainto_tsquery(...)`` should be able to use the
    GIN index and return matching rows.
    """
    org_id = await create_org(session_factory)
    kb_id = await _seed_kb(session_factory, org_id=org_id, name=f"tsv-{uuid.uuid4()}")
    entry_id = await _seed_wiki_entry(session_factory, kb_id)
    docs = [
        ("ripgrep is a command-line tool that recursively searches", 0),
        ("postgres provides full-text search via tsvector", 1),
        ("the quick brown fox jumps over the lazy dog", 2),
    ]
    async with session_factory() as db:
        for content, idx in docs:
            await db.execute(
                text(
                    "INSERT INTO kb_chunk "
                    "(id, wiki_entry_id, chunk_index, content) "
                    "VALUES (:id, :entry_id, :idx, :content)"
                ),
                {
                    "id": uuid.uuid4(),
                    "entry_id": entry_id,
                    "idx": idx,
                    "content": content,
                },
            )
        await db.commit()
        # Query: should match doc 1 (postgres) only.
        rows = (
            await db.execute(
                text(
                    "SELECT chunk_index FROM kb_chunk "
                    "WHERE wiki_entry_id = :eid "
                    "AND tsv @@ plainto_tsquery('english', 'tsvector') "
                    "ORDER BY chunk_index"
                ),
                {"eid": entry_id},
            )
        ).all()
    assert [r.chunk_index for r in rows] == [1]


async def test_kb_chunk_hnsw_cosine_query_returns_nearest(session_factory):
    """``ORDER BY embedding <=> :vec`` cosine-distance ordering should
    return chunks in nearest-first order. Uses small synthetic vectors
    that hit predictable distances.
    """
    org_id = await create_org(session_factory)
    kb_id = await _seed_kb(session_factory, org_id=org_id, name=f"hnsw-{uuid.uuid4()}")
    entry_id = await _seed_wiki_entry(session_factory, kb_id)

    def vec(*ones: int) -> str:
        v = [0.0] * 1024
        for i in ones:
            v[i] = 1.0
        return str(v)

    chunks = [
        # chunk_index, embedding hot indices
        (0, vec(0, 1, 2)),
        (1, vec(0, 1)),
        (2, vec(500, 501)),
        (3, vec(0, 1, 3)),
    ]
    async with session_factory() as db:
        for idx, emb in chunks:
            await db.execute(
                text(
                    "INSERT INTO kb_chunk "
                    "(id, wiki_entry_id, chunk_index, content, embedding) "
                    "VALUES (:id, :entry_id, :idx, :content, :emb)"
                ),
                {
                    "id": uuid.uuid4(),
                    "entry_id": entry_id,
                    "idx": idx,
                    "content": f"chunk {idx}",
                    "emb": emb,
                },
            )
        await db.commit()
        # Query vector close to (0, 1, 2) — chunk 0 should rank first.
        rows = (
            await db.execute(
                text(
                    "SELECT chunk_index FROM kb_chunk "
                    "WHERE wiki_entry_id = :eid "
                    "ORDER BY embedding <=> :vec "
                    "LIMIT 4"
                ),
                {"eid": entry_id, "vec": vec(0, 1, 2)},
            )
        ).all()
    order = [r.chunk_index for r in rows]
    assert order[0] == 0, f"closest chunk should be index 0, got {order}"
    # Chunk 2 is far away in the embedding space; should come last.
    assert order[-1] == 2, f"farthest chunk should be index 2, got {order}"


# ---------------------------------------------------------------------------
# Constraints: cascade, unique
# ---------------------------------------------------------------------------


async def test_cascade_delete_kb_drops_children(session_factory):
    """Deleting a kb row should cascade to all children (sources, raw
    docs, wiki entries, chunks). Verifies the ``ON DELETE CASCADE`` FK
    declarations in the ORM are wired correctly.
    """
    org_id = await create_org(session_factory)
    kb_id = await _seed_kb(session_factory, org_id=org_id, name=f"cascade-{uuid.uuid4()}")
    entry_id = await _seed_wiki_entry(session_factory, kb_id)
    src_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    raw_id = uuid.uuid4()
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO kb_source (id, kb_id, kind, config) "
                "VALUES (:id, :kb_id, 'markdown_dir', '{}')"
            ),
            {"id": src_id, "kb_id": kb_id},
        )
        await db.execute(
            text(
                "INSERT INTO kb_raw_doc "
                "(id, kb_id, source_id, path, content_sha) "
                "VALUES (:id, :kb_id, :sid, 'docs/x.md', 'sha')"
            ),
            {"id": raw_id, "kb_id": kb_id, "sid": src_id},
        )
        await db.execute(
            text(
                "INSERT INTO kb_chunk "
                "(id, wiki_entry_id, chunk_index, content) "
                "VALUES (:id, :eid, 0, 'x')"
            ),
            {"id": chunk_id, "eid": entry_id},
        )
        await db.commit()
        # Delete the parent.
        await db.execute(text("DELETE FROM kb WHERE id = :id"), {"id": kb_id})
        await db.commit()
        # All children should be gone.
        for table, where_id in [
            ("kb_source", src_id),
            ("kb_raw_doc", raw_id),
            ("kb_wiki_entry", entry_id),
            ("kb_chunk", chunk_id),
        ]:
            row = (
                await db.execute(
                    text(f"SELECT 1 FROM {table} WHERE id = :id"),
                    {"id": where_id},
                )
            ).first()
            assert row is None, f"{table} row not cascaded after kb delete"


async def test_kb_raw_doc_unique_kb_path(session_factory):
    """``UNIQUE (kb_id, path)`` should reject duplicate inserts."""
    org_id = await create_org(session_factory)
    kb_id = await _seed_kb(session_factory, org_id=org_id, name=f"uq-{uuid.uuid4()}")
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO kb_raw_doc "
                "(id, kb_id, path, content_sha) "
                "VALUES (:id, :kb_id, 'docs/x.md', 'sha1')"
            ),
            {"id": uuid.uuid4(), "kb_id": kb_id},
        )
        await db.commit()
    # Second insert with same (kb_id, path) must fail.
    with pytest.raises(IntegrityError):
        async with session_factory() as db:
            await db.execute(
                text(
                    "INSERT INTO kb_raw_doc "
                    "(id, kb_id, path, content_sha) "
                    "VALUES (:id, :kb_id, 'docs/x.md', 'sha2')"
                ),
                {"id": uuid.uuid4(), "kb_id": kb_id},
            )
            await db.commit()


async def test_kb_unique_org_name_allows_same_name_in_different_orgs(session_factory):
    """``uq_kb_org_name`` is a partial unique index where ``org_id IS NOT NULL``.
    Two orgs may both have a KB called ``acme-policies``; one org cannot
    have two KBs with the same name.
    """
    org_a = await create_org(session_factory)
    org_b = await create_org(session_factory)
    name = f"same-name-{uuid.uuid4()}"
    # Both orgs can have a KB called <name>.
    await _seed_kb(session_factory, org_id=org_a, name=name)
    await _seed_kb(session_factory, org_id=org_b, name=name)
    # But org A can't have two with that name.
    with pytest.raises(IntegrityError):
        await _seed_kb(session_factory, org_id=org_a, name=name)


# ---------------------------------------------------------------------------
# Idempotence: re-running apply_kb_ddl is a no-op
# ---------------------------------------------------------------------------


async def test_apply_kb_ddl_idempotent(engine, session_factory):
    """Re-running ``apply_kb_ddl`` on a populated DB should not raise
    and should not destroy existing data.
    """
    from surogates.db.engine import apply_kb_ddl

    org_id = await create_org(session_factory)
    kb_id = await _seed_kb(session_factory, org_id=org_id, name=f"idem-{uuid.uuid4()}")

    # Re-apply.
    async with engine.begin() as conn:
        await apply_kb_ddl(conn)

    # KB row still there.
    async with session_factory() as db:
        row = (
            await db.execute(text("SELECT id FROM kb WHERE id = :id"), {"id": kb_id})
        ).first()
    assert row is not None
