-- ============================================================================
-- KB (Knowledge Base) DDL: pgvector extension, GENERATED tsvector column,
-- BM25 GIN + pgvector HNSW indexes, row-level-security policies for tenant
-- isolation across the six KB tables.
--
-- Executed by ``apply_kb_ddl`` in ``surogates/db/engine.py`` after
-- ``Base.metadata.create_all``. Idempotent — every statement uses
-- IF NOT EXISTS / CREATE OR REPLACE / DROP IF EXISTS so re-running on a
-- populated database is a no-op.
--
-- Tenant scoping is via the per-session GUC ``app.org_id``. Application
-- code sets this at the start of every request:
--
--     SET LOCAL app.org_id = '<org-uuid>';
--
-- Without it, queries see only platform KBs (``kb.org_id IS NULL``).
-- ============================================================================


-- ----------------------------------------------------------------------------
-- pgvector extension. Required for the ``vector(1024)`` column on kb_chunk.
-- The extension MUST be installed before ``Base.metadata.create_all`` runs
-- (the ORM emits ``embedding vector(1024)`` for kb_chunk and Postgres needs
-- the type to exist). ``run_migrations`` in engine.py installs the extension
-- before create_all; this statement is a safety net for re-runs.
-- ----------------------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS vector;


-- ----------------------------------------------------------------------------
-- BM25 lexical search column on kb_chunk.
--
-- ``Base.metadata.create_all`` cannot create a GENERATED column with a
-- function call (``to_tsvector``), so we add it here. ``ADD COLUMN IF NOT
-- EXISTS`` keeps re-runs no-op. GIN-indexed so ``WHERE tsv @@ plainto_tsquery``
-- is index-backed.
-- ----------------------------------------------------------------------------

ALTER TABLE kb_chunk
    ADD COLUMN IF NOT EXISTS tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;

CREATE INDEX IF NOT EXISTS idx_kb_chunk_tsv
    ON kb_chunk USING gin(tsv);


-- ----------------------------------------------------------------------------
-- pgvector HNSW index on kb_chunk.embedding for fast cosine-similarity ANN.
--
-- ``m=16, ef_construction=64`` are conservative defaults appropriate for
-- KBs up to ~100k chunks. Beyond that, drop and rebuild with CONCURRENTLY
-- and tune. NULL embeddings (chunks pending re-embedding during reindex)
-- are skipped by HNSW.
-- ----------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_kb_chunk_embedding
    ON kb_chunk USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);


-- ----------------------------------------------------------------------------
-- Row-level security across the six KB tables.
--
-- This is the third layer of tenant isolation, behind:
--   1. Tools deriving ``org_id`` from kwargs (never from a tool argument).
--   2. Every application query joining ``kb`` and filtering ``kb.org_id``.
-- RLS is the backstop: even a query that forgets to filter cannot leak
-- another tenant's rows.
--
-- Policies use ``current_setting('app.org_id', true)`` — the second arg
-- ``true`` returns NULL when the GUC is unset rather than raising. The
-- ``NULLIF(..., '')::uuid`` cast handles both unset (NULL) and empty-string
-- ('') as "no org context", in which case only platform KBs are visible.
--
-- ``FORCE ROW LEVEL SECURITY`` makes RLS apply to the table owner too — so
-- even ad-hoc queries from an admin role respect tenancy. DDL (this script)
-- is exempt; only DML is filtered.
--
-- Superuser caveat: Postgres roles with ``rolsuper`` or ``rolbypassrls`` set
-- skip RLS entirely (FORCE doesn't help — it only forces RLS on table owners
-- who are NOT superusers). The application connection MUST be a normal role
-- with neither attribute. Migrations run as a privileged role; the runtime
-- API/worker connection is a separate, RLS-subject role. In local dev, the
-- ``postgres`` image's default user is a superuser — to exercise RLS, query
-- via a ``CREATE ROLE app NOLOGIN NOBYPASSRLS`` test role with ``SET ROLE``.
-- ----------------------------------------------------------------------------

-- Top-level: kb itself.
ALTER TABLE kb ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS kb_tenant_iso ON kb;
CREATE POLICY kb_tenant_iso ON kb
    USING (
        org_id IS NULL
        OR org_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    );

-- Children of kb: filter via the parent kb's tenant scope. The subquery is
-- inlined by the planner so the cost is one extra index lookup per query.

ALTER TABLE kb_source ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb_source FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS kb_source_tenant_iso ON kb_source;
CREATE POLICY kb_source_tenant_iso ON kb_source
    USING (
        kb_id IN (
            SELECT id FROM kb
            WHERE org_id IS NULL
               OR org_id = NULLIF(current_setting('app.org_id', true), '')::uuid
        )
    );

ALTER TABLE kb_raw_doc ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb_raw_doc FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS kb_raw_doc_tenant_iso ON kb_raw_doc;
CREATE POLICY kb_raw_doc_tenant_iso ON kb_raw_doc
    USING (
        kb_id IN (
            SELECT id FROM kb
            WHERE org_id IS NULL
               OR org_id = NULLIF(current_setting('app.org_id', true), '')::uuid
        )
    );

ALTER TABLE kb_wiki_entry ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb_wiki_entry FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS kb_wiki_entry_tenant_iso ON kb_wiki_entry;
CREATE POLICY kb_wiki_entry_tenant_iso ON kb_wiki_entry
    USING (
        kb_id IN (
            SELECT id FROM kb
            WHERE org_id IS NULL
               OR org_id = NULLIF(current_setting('app.org_id', true), '')::uuid
        )
    );

-- kb_chunk hangs off kb_wiki_entry; walk via the join to reach kb.org_id.
ALTER TABLE kb_chunk ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb_chunk FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS kb_chunk_tenant_iso ON kb_chunk;
CREATE POLICY kb_chunk_tenant_iso ON kb_chunk
    USING (
        wiki_entry_id IN (
            SELECT we.id
            FROM kb_wiki_entry we
            JOIN kb ON kb.id = we.kb_id
            WHERE kb.org_id IS NULL
               OR kb.org_id = NULLIF(current_setting('app.org_id', true), '')::uuid
        )
    );

-- agent_kb_grant: the grant row is itself tenant-scoped via its kb.
ALTER TABLE agent_kb_grant ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_kb_grant FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS agent_kb_grant_tenant_iso ON agent_kb_grant;
CREATE POLICY agent_kb_grant_tenant_iso ON agent_kb_grant
    USING (
        kb_id IN (
            SELECT id FROM kb
            WHERE org_id IS NULL
               OR org_id = NULLIF(current_setting('app.org_id', true), '')::uuid
        )
    );
