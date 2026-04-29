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
-- Backstop layer. Behind:
--   1. Tools deriving ``org_id`` from kwargs (never from a tool argument).
--   2. Every application query joining ``kb`` and filtering ``kb.org_id``.
-- RLS catches anything that slips through.
--
-- Per table we install TWO permissive policies (UNION semantics):
--
--   *_read_with_platform — FOR SELECT only, USING permits BOTH platform
--                          rows (org_id IS NULL) and the current tenant's
--                          own rows. Lets clients read platform-shipped
--                          KBs (e.g. "invergent-docs") while still
--                          isolating across tenants.
--
--   *_strict_own         — FOR ALL, USING + WITH CHECK both restrict to
--                          ``org_id = GUC AND org_id IS NOT NULL``. Locks
--                          INSERT/UPDATE/DELETE to the tenant's own rows.
--                          Platform-row writes from the application role
--                          are rejected; platform content is seeded by a
--                          privileged migration/admin role that bypasses
--                          RLS.
--
-- Net effect, per command:
--   SELECT  — visible if read_with_platform OR strict_own matches
--             (platform rows visible; own rows visible; other tenants hidden)
--   INSERT  — only strict_own applies (only own-tenant rows accepted)
--   UPDATE  — only strict_own applies on USING (only own-tenant existing rows)
--             AND on WITH CHECK (cannot move row to another org or to NULL)
--   DELETE  — only strict_own applies (cannot delete platform or other-tenant)
--
-- ``current_setting('app.org_id', true)`` — the ``true`` second arg returns
-- NULL when the GUC is unset rather than raising. ``NULLIF(..., '')::uuid``
-- treats unset OR empty-string as "no tenant", in which case only platform
-- rows match read_with_platform and strict_own matches nothing.
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

-- Drop any prior single-policy form left from earlier revisions of this
-- script before installing the split read/write policy pair below.
DROP POLICY IF EXISTS kb_tenant_iso              ON kb;
DROP POLICY IF EXISTS kb_source_tenant_iso       ON kb_source;
DROP POLICY IF EXISTS kb_raw_doc_tenant_iso      ON kb_raw_doc;
DROP POLICY IF EXISTS kb_wiki_entry_tenant_iso   ON kb_wiki_entry;
DROP POLICY IF EXISTS kb_chunk_tenant_iso        ON kb_chunk;
DROP POLICY IF EXISTS agent_kb_grant_tenant_iso  ON agent_kb_grant;

-- ===== kb =====================================================================
ALTER TABLE kb ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS kb_read_with_platform ON kb;
CREATE POLICY kb_read_with_platform ON kb
    FOR SELECT
    USING (
        org_id IS NULL
        OR org_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    );

DROP POLICY IF EXISTS kb_strict_own ON kb;
CREATE POLICY kb_strict_own ON kb
    FOR ALL
    USING (
        org_id IS NOT NULL
        AND org_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    )
    WITH CHECK (
        org_id IS NOT NULL
        AND org_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    );

-- ===== kb_source ==============================================================
-- Children of kb: filter via the parent kb's tenant scope. The subquery is
-- inlined by the planner so the cost is one extra index lookup per query.
ALTER TABLE kb_source ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb_source FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS kb_source_read_with_platform ON kb_source;
CREATE POLICY kb_source_read_with_platform ON kb_source
    FOR SELECT
    USING (
        kb_id IN (
            SELECT id FROM kb
            WHERE org_id IS NULL
               OR org_id = NULLIF(current_setting('app.org_id', true), '')::uuid
        )
    );

DROP POLICY IF EXISTS kb_source_strict_own ON kb_source;
CREATE POLICY kb_source_strict_own ON kb_source
    FOR ALL
    USING (
        kb_id IN (
            SELECT id FROM kb
            WHERE org_id IS NOT NULL
               AND org_id = NULLIF(current_setting('app.org_id', true), '')::uuid
        )
    )
    WITH CHECK (
        kb_id IN (
            SELECT id FROM kb
            WHERE org_id IS NOT NULL
               AND org_id = NULLIF(current_setting('app.org_id', true), '')::uuid
        )
    );

-- ===== kb_raw_doc =============================================================
ALTER TABLE kb_raw_doc ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb_raw_doc FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS kb_raw_doc_read_with_platform ON kb_raw_doc;
CREATE POLICY kb_raw_doc_read_with_platform ON kb_raw_doc
    FOR SELECT
    USING (
        kb_id IN (
            SELECT id FROM kb
            WHERE org_id IS NULL
               OR org_id = NULLIF(current_setting('app.org_id', true), '')::uuid
        )
    );

DROP POLICY IF EXISTS kb_raw_doc_strict_own ON kb_raw_doc;
CREATE POLICY kb_raw_doc_strict_own ON kb_raw_doc
    FOR ALL
    USING (
        kb_id IN (
            SELECT id FROM kb
            WHERE org_id IS NOT NULL
               AND org_id = NULLIF(current_setting('app.org_id', true), '')::uuid
        )
    )
    WITH CHECK (
        kb_id IN (
            SELECT id FROM kb
            WHERE org_id IS NOT NULL
               AND org_id = NULLIF(current_setting('app.org_id', true), '')::uuid
        )
    );

-- ===== kb_wiki_entry ==========================================================
ALTER TABLE kb_wiki_entry ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb_wiki_entry FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS kb_wiki_entry_read_with_platform ON kb_wiki_entry;
CREATE POLICY kb_wiki_entry_read_with_platform ON kb_wiki_entry
    FOR SELECT
    USING (
        kb_id IN (
            SELECT id FROM kb
            WHERE org_id IS NULL
               OR org_id = NULLIF(current_setting('app.org_id', true), '')::uuid
        )
    );

DROP POLICY IF EXISTS kb_wiki_entry_strict_own ON kb_wiki_entry;
CREATE POLICY kb_wiki_entry_strict_own ON kb_wiki_entry
    FOR ALL
    USING (
        kb_id IN (
            SELECT id FROM kb
            WHERE org_id IS NOT NULL
               AND org_id = NULLIF(current_setting('app.org_id', true), '')::uuid
        )
    )
    WITH CHECK (
        kb_id IN (
            SELECT id FROM kb
            WHERE org_id IS NOT NULL
               AND org_id = NULLIF(current_setting('app.org_id', true), '')::uuid
        )
    );

-- ===== kb_chunk ===============================================================
-- kb_chunk hangs off kb_wiki_entry; walk via the join to reach kb.org_id.
ALTER TABLE kb_chunk ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb_chunk FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS kb_chunk_read_with_platform ON kb_chunk;
CREATE POLICY kb_chunk_read_with_platform ON kb_chunk
    FOR SELECT
    USING (
        wiki_entry_id IN (
            SELECT we.id
            FROM kb_wiki_entry we
            JOIN kb ON kb.id = we.kb_id
            WHERE kb.org_id IS NULL
               OR kb.org_id = NULLIF(current_setting('app.org_id', true), '')::uuid
        )
    );

DROP POLICY IF EXISTS kb_chunk_strict_own ON kb_chunk;
CREATE POLICY kb_chunk_strict_own ON kb_chunk
    FOR ALL
    USING (
        wiki_entry_id IN (
            SELECT we.id
            FROM kb_wiki_entry we
            JOIN kb ON kb.id = we.kb_id
            WHERE kb.org_id IS NOT NULL
               AND kb.org_id = NULLIF(current_setting('app.org_id', true), '')::uuid
        )
    )
    WITH CHECK (
        wiki_entry_id IN (
            SELECT we.id
            FROM kb_wiki_entry we
            JOIN kb ON kb.id = we.kb_id
            WHERE kb.org_id IS NOT NULL
               AND kb.org_id = NULLIF(current_setting('app.org_id', true), '')::uuid
        )
    );

-- ===== agent_kb_grant =========================================================
-- The grant row is tenant-scoped via its kb. Note: granting a platform KB
-- to an agent is unnecessary (platform KBs are implicitly visible to all
-- agents) and is rejected by strict_own's WITH CHECK.
ALTER TABLE agent_kb_grant ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_kb_grant FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS agent_kb_grant_read_with_platform ON agent_kb_grant;
CREATE POLICY agent_kb_grant_read_with_platform ON agent_kb_grant
    FOR SELECT
    USING (
        kb_id IN (
            SELECT id FROM kb
            WHERE org_id IS NULL
               OR org_id = NULLIF(current_setting('app.org_id', true), '')::uuid
        )
    );

DROP POLICY IF EXISTS agent_kb_grant_strict_own ON agent_kb_grant;
CREATE POLICY agent_kb_grant_strict_own ON agent_kb_grant
    FOR ALL
    USING (
        kb_id IN (
            SELECT id FROM kb
            WHERE org_id IS NOT NULL
               AND org_id = NULLIF(current_setting('app.org_id', true), '')::uuid
        )
    )
    WITH CHECK (
        kb_id IN (
            SELECT id FROM kb
            WHERE org_id IS NOT NULL
               AND org_id = NULLIF(current_setting('app.org_id', true), '')::uuid
        )
    );
