-- ============================================================================
-- Observability DDL: schema fixups + trigger + views for database-level
-- audit/training access.
--
-- This file is executed after ``Base.metadata.create_all`` and is idempotent
-- (every statement uses CREATE OR REPLACE / DROP IF EXISTS / ADD COLUMN
-- IF NOT EXISTS).  External BI and observability tools read these views
-- directly; the platform does not expose an HTTP API for observability.
--
-- See ``docs/audit/events.md`` for the stable JSONB schema of each
-- event type that the views unpack.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- Schema fixups — columns and indexes that ``Base.metadata.create_all``
-- cannot retrofit onto already-existing tables.  Each statement is
-- guarded so re-running is a no-op on fresh databases (where the ORM
-- has already put the column or index in place) and on upgraded ones.
-- ----------------------------------------------------------------------------

ALTER TABLE events
    ADD COLUMN IF NOT EXISTS org_id  uuid REFERENCES orgs(id),
    ADD COLUMN IF NOT EXISTS user_id uuid REFERENCES users(id);

-- When the device link first welcomed a device (``Device.connected_at``). A
-- device seen before the column existed had connected: it counts as such, once,
-- when the column is added, and never again, as later sightings need no welcome.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'devices' AND column_name = 'connected_at'
    ) THEN
        ALTER TABLE devices ADD COLUMN connected_at timestamptz;
        UPDATE devices SET connected_at = last_seen_at WHERE last_seen_at IS NOT NULL;
    END IF;
END $$;

-- When a device's token was last reissued (``Device.reauthorized_at``): none was dated before.
ALTER TABLE devices ADD COLUMN IF NOT EXISTS reauthorized_at timestamptz;

-- The computer and folder a project made in Surogate Desktop works on (``Workstream.device_id``,
-- ``Workstream.folder``): a project made before them was made in the cloud, and stays there.
ALTER TABLE workstreams
    ADD COLUMN IF NOT EXISTS device_id uuid,
    ADD COLUMN IF NOT EXISTS folder text;

-- Retrofit for databases created before ``idx_device_operations_session_kind`` joined the model: what a session asked
-- of one kind, by when it closed.  A landing on a computer reads a thread's commands, writes and records since its last
-- landing; without this it reads every operation the thread ever asked, at every landing.
CREATE INDEX IF NOT EXISTS idx_device_operations_session_kind
    ON device_operations (calling_session_id, kind, completed_at);

-- Retrofit for databases created before ``idx_workstream_history_undoes`` joined the model: the Undos a History, a
-- thread's row or an Undo reads, those naming its records and the Undos of those, found by the records each undoes,
-- not by a walk of every record of the project at each step.
CREATE INDEX IF NOT EXISTS idx_workstream_history_undoes
    ON workstream_history USING gin (undoes) WHERE kind = 'undo';

CREATE INDEX IF NOT EXISTS idx_events_audit_type_time
    ON events (org_id, type, created_at);

CREATE INDEX IF NOT EXISTS idx_events_audit_user_time
    ON events (org_id, user_id, type, created_at);

CREATE INDEX IF NOT EXISTS idx_events_session_type
    ON events (session_id, type);

-- Retrofit for databases created before ``idx_sessions_parent`` joined the
-- model: create_all adds missing tables, never missing indexes on tables
-- that already exist.  Anything that walks a conversation's spawned work
-- recurses on parent_id, and each level of that recursion is a lookup —
-- without this it is a sequential scan per level.
CREATE INDEX IF NOT EXISTS idx_sessions_parent
    ON sessions (parent_id);

-- Retrofit for the references into events and sessions, which agent and
-- session deletion remove in bulk.  Postgres checks each removed row against
-- every referencing table; an unindexed reference makes each check a
-- sequential scan of that table.  Deleting a 240k-event agent scanned
-- delivery_outbox 240k times before idx_delivery_outbox_event existed.
CREATE INDEX IF NOT EXISTS idx_delivery_outbox_event
    ON delivery_outbox (event_id);

CREATE INDEX IF NOT EXISTS idx_delivery_outbox_session
    ON delivery_outbox (session_id);

CREATE INDEX IF NOT EXISTS idx_board_notes_writer_session
    ON board_notes (writer_session_id);

CREATE INDEX IF NOT EXISTS idx_workstream_history_thread
    ON workstream_history (thread_id);

-- Backing index for ``SessionStore.find_feedback_on_event`` — the
-- feedback dedupe query filters on (session_id, type, target_event_id,
-- and either rated_by_user_id or rated_by_service_account_id).  Without
-- this expression index the query re-evaluates ``data->>`` on every row
-- of (session_id, type), which turns into a hot-path seq-scan once a
-- judge starts grading pipeline output at steady state.
CREATE INDEX IF NOT EXISTS idx_events_feedback_dedupe
    ON events (
        session_id,
        type,
        (data->>'target_event_id'),
        (data->>'rated_by_user_id'),
        (data->>'rated_by_service_account_id')
    )
    WHERE type IN ('user.feedback', 'expert.endorse', 'expert.override');

-- Backing index for cross-session narrative search (the ``session_search``
-- builtin and the control plane's operator-facing search).  Without it the
-- query evaluates ``to_tsvector`` per row over a table that is ~90%
-- streamed ``llm.delta`` chunks — a full sequential scan per search.
--
-- The expression and the type predicate MUST stay identical to
-- ``surogates.db.narrative``: Postgres only uses an expression index when
-- the query's expression parses to the same tree, and only uses a partial
-- index when the query's predicate implies this WHERE clause.  Drift is
-- silent — it just stops using the index.  ``tests/db/test_narrative_index.py``
-- pins this statement against the helpers.
--
-- CHANGING THE EXPRESSION REQUIRES A NEW INDEX NAME.  ``IF NOT EXISTS``
-- matches on name alone, so an edited expression under the same name is a
-- no-op on every database that already has the index: the tests pass (they
-- run against a fresh schema) while production keeps the old definition and
-- quietly stops using it.  Bump the ``_v2`` suffix and drop the previous
-- index once the new one is built.
--
-- ``left(..., 500000)`` is not cosmetic: a tsvector may hold at most
-- 1,048,575 bytes of lexemes and overflowing it is an ERROR.  ``user.message``
-- bodies are unbounded, so without the cap one long message makes its own
-- INSERT fail once this index exists, and makes this CREATE INDEX abort —
-- rolling back every statement in this file — if such a row already exists.
CREATE INDEX IF NOT EXISTS idx_events_narrative_fts_v2
    ON events USING gin (
        to_tsvector('simple', left(COALESCE(data->>'content', '') || ' ' || COALESCE(data->'message'->>'content', '') || ' ' || COALESCE(data->>'recap', '') || ' ' || COALESCE(data->>'summary', ''), 500000))
    )
    WHERE type IN ('user.message', 'llm.response', 'tool.result', 'turn.summary', 'iteration.summary');

-- Per-tenant audit attribution.
-- ``agent_id`` is nullable so emitters with no per-tenant context
-- (e.g. platform-wide events) can leave it NULL.  The
-- (agent_id, created_at) index backs the per-tenant audit dashboards
-- that filter on a single agent over a time window.
ALTER TABLE audit_log
    ADD COLUMN IF NOT EXISTS agent_id text;

CREATE INDEX IF NOT EXISTS idx_audit_log_agent_time
    ON audit_log (agent_id, created_at);


-- Orphan-sweep backing index.  ``Orchestrator._sweep_orphans_*``
-- (dispatcher.py) re-enqueues sessions whose worker died mid-turn.
-- The query filters on ``status = 'active' AND updated_at < cutoff``
-- and is fired on a 60s timer by every replica plus a one-shot
-- aggressive boot sweep on each worker startup.  Without an index,
-- shared-mode workers (no ``agent_id`` filter) full-scan the sessions
-- table on every sweep — at 1M+ sessions and 50 replicas booting
-- simultaneously the DB grinds.
--
-- A PARTIAL index over the active rows only is much smaller (active
-- sessions are typically <1% of total) and is ordered by the exact
-- predicate the sweep uses (``updated_at < cutoff``) so the planner
-- can index-range-scan and stop at the first non-stale row.  Sub-100ms
-- regardless of total session count.
CREATE INDEX IF NOT EXISTS idx_sessions_active_updated
    ON sessions (updated_at)
    WHERE status = 'active';


-- ----------------------------------------------------------------------------
-- Service accounts — API-channel auth (programmatic clients).
--
-- ``Base.metadata.create_all`` creates this table on fresh databases.  The
-- CREATE here is the retrofit for existing deployments that pre-date the
-- table.  ``IF NOT EXISTS`` makes both paths a no-op after the first run.
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS service_accounts (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        uuid NOT NULL REFERENCES orgs(id),
    name          text NOT NULL,
    token_hash    text NOT NULL UNIQUE,
    token_prefix  text NOT NULL,
    created_at    timestamp NOT NULL DEFAULT now(),
    last_used_at  timestamp,
    revoked_at    timestamp
);

CREATE INDEX IF NOT EXISTS idx_service_accounts_org
    ON service_accounts (org_id);

-- Per-agent principal: ``agent_id`` links a service account to its owning ops
-- Agent (a different database — logical reference, not a FK).  Retrofit for
-- existing databases; ``create_all`` covers fresh ones.
ALTER TABLE service_accounts
    ADD COLUMN IF NOT EXISTS agent_id text;

-- ``kind`` splits the agent's own machine identity from the operator-minted
-- API keys of the OpenAI-compatible channel, which share this table and its
-- auth path but obey opposite cardinality rules.  See the ``ServiceAccount``
-- model docstring for the vocabulary.
ALTER TABLE service_accounts
    ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'service';

-- Every row that carried an ``agent_id`` before ``kind`` existed was, by
-- construction, an agent principal — nothing else could write that column.
-- Settles to a no-op after the first run: every writer now sets ``kind``
-- explicitly, so no later row can match both halves of this predicate.
--
-- Promotes at most ONE row per agent. During a rolling deploy a replica still
-- running pre-``kind`` code inserts a principal with the column default, so an
-- agent can briefly hold two such rows; promoting both would make the unique
-- index below impossible to build and abort this whole script inside its
-- transaction, taking the service down on the next start. The newest row wins,
-- matching what the old writer intended.
--
-- A straggler left at 'service' is NOT inert: every principal lookup filters
-- on kind, so an agent whose only row stayed 'service' has no resolvable
-- identity and its turns fail. That is why the promotion runs on every start
-- rather than once — a row written by an old replica after this script ran is
-- picked up by the next boot — and why the query below reports any that are
-- still waiting.
UPDATE service_accounts sa
   SET kind = 'agent_principal'
  FROM (
        SELECT DISTINCT ON (agent_id) id
          FROM service_accounts
         WHERE agent_id IS NOT NULL AND kind = 'service'
         ORDER BY agent_id, created_at DESC, id DESC
       ) newest
 WHERE sa.id = newest.id
   AND NOT EXISTS (
        SELECT 1 FROM service_accounts other
         WHERE other.agent_id = sa.agent_id
           AND other.kind = 'agent_principal'
       );

-- The old index capped an agent at ONE service account, which made a second
-- API key impossible.  Replaced by a narrower predicate that constrains only
-- the principal.  Dropped by name: on a fresh database ``create_all`` has
-- already built the replacement, so this is a no-op there.
DROP INDEX IF EXISTS uq_service_accounts_agent;

CREATE UNIQUE INDEX IF NOT EXISTS uq_service_accounts_agent_principal
    ON service_accounts (agent_id)
    WHERE agent_id IS NOT NULL AND kind = 'agent_principal';

CREATE INDEX IF NOT EXISTS idx_service_accounts_agent
    ON service_accounts (agent_id)
    WHERE agent_id IS NOT NULL;

-- One ``case_type`` account per case type per org: the identity a Keel case
-- type's agent-step sessions run under.  Two concurrent first steps of the same
-- case type race to create it; this index lets exactly one win.  Retrofit for
-- existing databases; ``create_all`` covers fresh ones.
CREATE UNIQUE INDEX IF NOT EXISTS uq_service_accounts_case_type
    ON service_accounts (org_id, name)
    WHERE kind = 'case_type';

-- Surfaces an agent whose principal never got promoted: it has an agent_id,
-- no principal, and therefore no identity the runtime can resolve. Expected
-- to return nothing; a row here means a pre-``kind`` writer landed after the
-- promotion above and the next start will fix it.
DO $$
DECLARE stranded integer;
BEGIN
    SELECT count(*) INTO stranded
      FROM service_accounts sa
     WHERE sa.agent_id IS NOT NULL
       AND sa.revoked_at IS NULL
       AND NOT EXISTS (
            SELECT 1 FROM service_accounts p
             WHERE p.agent_id = sa.agent_id
               AND p.kind = 'agent_principal'
               AND p.revoked_at IS NULL
           );
    IF stranded > 0 THEN
        RAISE WARNING
            'service_accounts: % agent row(s) have no agent_principal; '
            'those agents cannot resolve an identity until this script '
            'runs again after the old replicas are gone', stranded;
    END IF;
END $$;

-- ----------------------------------------------------------------------------
-- Sessions — retrofits for the API channel.
--
-- ``user_id`` becomes nullable (API-channel sessions are owned by a service
-- account, not a user).  ``service_account_id`` points at the owning SA
-- when set.  ``idempotency_key`` lets the fire-and-forget
-- ``POST /v1/api/prompts`` endpoint dedupe retries via a partial unique
-- index.  Each statement is guarded so re-running the DDL is a no-op.
-- ----------------------------------------------------------------------------

ALTER TABLE sessions
    ALTER COLUMN user_id DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS service_account_id uuid REFERENCES service_accounts(id),
    ADD COLUMN IF NOT EXISTS idempotency_key    text;

CREATE INDEX IF NOT EXISTS idx_sessions_service_account
    ON sessions (service_account_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_idempotency
    ON sessions (org_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_scheduled_sessions_user
    ON scheduled_sessions (org_id, user_id, agent_id);

CREATE INDEX IF NOT EXISTS idx_scheduled_sessions_due
    ON scheduled_sessions (agent_id, status, next_run_at)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_scheduled_sessions_lock
    ON scheduled_sessions (locked_until);

-- expire_active_schedules (the per-tick expiry sweep) filters+orders active
-- schedules by expires_at.  Partial index over active rows only so the planner
-- range-scans the expired rows instead of scanning + sorting the active set.
CREATE INDEX IF NOT EXISTS idx_scheduled_sessions_expiry
    ON scheduled_sessions (expires_at)
    WHERE status = 'active';


-- BYO Firebase self-registration: namespaced ``auth_provider`` plus
-- partial unique index guarantees that two BYO Firebase projects can
-- mint the same UID without colliding inside the users table.
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_org_auth_external
    ON users (org_id, auth_provider, external_id)
    WHERE external_id IS NOT NULL;


-- ----------------------------------------------------------------------------
-- Channel identities: org-scoped retrofit.
--
-- channel_identities moved from a GLOBAL (platform, platform_user_id)
-- uniqueness to an org-scoped identity, so the same platform user (e.g. a Slack
-- workspace member) resolves to its own user per tenant instead of leaking
-- across orgs.  The ORM carries the org_id column + uq_channel_org_platform to
-- fresh databases; this backfills org_id and swaps the constraint on
-- already-deployed ones.
-- ----------------------------------------------------------------------------
ALTER TABLE channel_identities ADD COLUMN IF NOT EXISTS org_id uuid;
UPDATE channel_identities ci SET org_id = u.org_id
    FROM users u WHERE ci.user_id = u.id AND ci.org_id IS NULL;
ALTER TABLE channel_identities DROP CONSTRAINT IF EXISTS uq_channel_platform;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'channel_identities_org_id_fkey') THEN
        ALTER TABLE channel_identities
            ADD CONSTRAINT channel_identities_org_id_fkey FOREIGN KEY (org_id) REFERENCES orgs(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_channel_org_platform') THEN
        ALTER TABLE channel_identities
            ADD CONSTRAINT uq_channel_org_platform UNIQUE (org_id, platform, platform_user_id);
    END IF;
END $$;
ALTER TABLE channel_identities ALTER COLUMN org_id SET NOT NULL;


-- ----------------------------------------------------------------------------
-- Credentials uniqueness retrofit.
--
-- ``CredentialVault.store`` is an upsert keyed on
-- (org_id, user_id, service_account_id, name).  Without a unique index,
-- concurrent stores raced and produced duplicate rows, after which every
-- ``retrieve`` raised MultipleResultsFound.  The ORM ``UniqueConstraint`` on
-- ``Credential`` carries this to fresh databases; the dedupe + idempotent
-- ALTERs below retrofit already-deployed databases and add the agent
-- service-account scope.
--
-- ``NULLS NOT DISTINCT`` (PG 15+) makes rows whose principal columns are NULL
-- collide with each other, which is what the upsert needs for org-scoped
-- credentials (the dominant case).
-- ----------------------------------------------------------------------------

ALTER TABLE credentials
    ADD COLUMN IF NOT EXISTS service_account_id uuid REFERENCES service_accounts(id);

DELETE FROM credentials a
USING credentials b
WHERE a.created_at < b.created_at
  AND a.org_id = b.org_id
  AND a.name   = b.name
  AND a.user_id IS NOT DISTINCT FROM b.user_id
  AND a.service_account_id IS NOT DISTINCT FROM b.service_account_id;

ALTER TABLE credentials
    DROP CONSTRAINT IF EXISTS uq_credentials_org_user_name;

DROP INDEX IF EXISTS uq_credentials_org_user_name;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_credentials_org_user_sa_name'
    ) THEN
        ALTER TABLE credentials
            ADD CONSTRAINT uq_credentials_org_user_sa_name
            UNIQUE NULLS NOT DISTINCT (org_id, user_id, service_account_id, name);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_credentials_one_principal'
    ) THEN
        ALTER TABLE credentials
            ADD CONSTRAINT ck_credentials_one_principal
            CHECK (NOT (user_id IS NOT NULL AND service_account_id IS NOT NULL));
    END IF;
END $$;


-- ----------------------------------------------------------------------------
-- Tenant denormalization trigger
--
-- The ``events`` table carries ``org_id`` and ``user_id`` copied from the
-- owning session so audit queries can filter by tenant without joining
-- ``sessions``.  This trigger populates them on insert when the caller has
-- not set them explicitly.  It never overwrites a value the caller provided.
-- ----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION events_populate_tenant() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    _org_id uuid;
    _user_id uuid;
BEGIN
    IF NEW.org_id IS NULL OR NEW.user_id IS NULL THEN
        SELECT s.org_id, s.user_id INTO _org_id, _user_id
        FROM sessions s WHERE s.id = NEW.session_id;

        IF NEW.org_id IS NULL THEN
            NEW.org_id := _org_id;
        END IF;
        IF NEW.user_id IS NULL THEN
            NEW.user_id := _user_id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS events_populate_tenant_trg ON events;
CREATE TRIGGER events_populate_tenant_trg
    BEFORE INSERT ON events
    FOR EACH ROW
    EXECUTE FUNCTION events_populate_tenant();


-- ----------------------------------------------------------------------------
-- Subagent task layer — retrofits.
--
-- ``Base.metadata.create_all`` creates ``tasks`` and ``task_links`` on
-- fresh databases, but does not add the ``sessions.task_id`` column to
-- an already-existing ``sessions`` table. Each statement is guarded so
-- re-running the DDL is a no-op on fresh and upgraded databases alike.
--
-- See ``surogates/db/models.py`` (``Task``, ``TaskLink``, ``Session.task_id``)
-- and ``docs/sub-agents/2026-05-16-subagent-task-layer-v1.md`` for the
-- design rationale.
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tasks (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id             uuid NOT NULL REFERENCES orgs(id),
    parent_session_id  uuid NOT NULL REFERENCES sessions(id),
    agent_def_name     text,
    goal               text NOT NULL,
    context            text,
    current_session_id uuid REFERENCES sessions(id),
    status             text NOT NULL DEFAULT 'todo',
    result             text,
    blocked_reason     text,
    attempt_count      integer NOT NULL DEFAULT 0,
    max_attempts       integer NOT NULL DEFAULT 3,
    created_at         timestamp NOT NULL DEFAULT now(),
    started_at         timestamp,
    completed_at       timestamp
);

CREATE TABLE IF NOT EXISTS task_links (
    parent_id uuid NOT NULL REFERENCES tasks(id),
    child_id  uuid NOT NULL REFERENCES tasks(id),
    PRIMARY KEY (parent_id, child_id)
);

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS task_id uuid REFERENCES tasks(id);

-- v1.5: structured handoff metadata (set by the task_complete self-tool;
-- plain workers that complete naturally leave it NULL).
ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS result_metadata jsonb;

CREATE INDEX IF NOT EXISTS idx_tasks_org_status
    ON tasks (org_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_parent_session
    ON tasks (parent_session_id);
CREATE INDEX IF NOT EXISTS idx_tasks_current_session
    ON tasks (current_session_id);


-- ----------------------------------------------------------------------------
-- Mission layer (orchestrated goals) — retrofits.
--
-- ``Base.metadata.create_all`` creates ``missions`` and the indexes on
-- fresh databases, but does NOT add ``tasks.mission_id`` to an existing
-- ``tasks`` table. Each statement guarded for idempotent re-runs.
--
-- See ``surogates/db/models.py`` (``Mission``, ``Task.mission_id``) and
-- ``docs/superpowers/specs/2026-05-16-mission-orchestrated-goals-design.md``.
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS missions (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id                      uuid NOT NULL REFERENCES orgs(id),
    user_id                     uuid REFERENCES users(id),
    service_account_id          uuid REFERENCES service_accounts(id),
    session_id                  uuid NOT NULL REFERENCES sessions(id),
    agent_id                    text NOT NULL,
    description                 text NOT NULL,
    rubric                      text NOT NULL,
    status                      text NOT NULL DEFAULT 'active',
    iteration                   integer NOT NULL DEFAULT 0,
    max_iterations              integer NOT NULL DEFAULT 20,
    last_evaluation_result      text,
    last_evaluation_explanation text,
    last_evaluation_feedback    text,
    last_evaluation_at          timestamptz,
    evaluator_parse_failures    integer NOT NULL DEFAULT 0,
    paused_reason               text,
    cancelled_reason            text,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_missions_one_principal
        CHECK ((user_id IS NOT NULL)::int
             + (service_account_id IS NOT NULL)::int = 1)
);

-- ----------------------------------------------------------------------------
-- Missions — relax single-principal ownership.  ``user_id`` was NOT NULL with
-- a FK to users(id) — only user JWTs could own missions.  Sessions created
-- through ops's Work UI authenticate as per-user service accounts (the
-- surogates session row has user_id=NULL, service_account_id=<sa>), so they
-- could never start a /mission.
--
-- Drop NOT NULL on user_id, add service_account_id, and enforce the
-- principal invariant (exactly one of the two is set) via CHECK.
-- ----------------------------------------------------------------------------

ALTER TABLE missions
    ALTER COLUMN user_id DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS service_account_id uuid
        REFERENCES service_accounts(id),
    ADD COLUMN IF NOT EXISTS budget_tokens integer,
    -- Consecutive evaluations that reported no progress.
    ADD COLUMN IF NOT EXISTS stagnant_evaluations integer NOT NULL DEFAULT 0;

-- Replace the old (org, user, agent, status) index with one that covers
-- both principal shapes.  Old index is dropped explicitly because the
-- name is changing.
DROP INDEX IF EXISTS idx_missions_user_agent_status;
CREATE INDEX IF NOT EXISTS idx_missions_principal_agent_status
    ON missions (org_id, user_id, service_account_id, agent_id, status);

-- Exactly one principal must be set.  Wrapped in DO block so the ADD is
-- idempotent (PG has no ADD CONSTRAINT IF NOT EXISTS).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_missions_one_principal'
          AND conrelid = 'missions'::regclass
    ) THEN
        ALTER TABLE missions
            ADD CONSTRAINT ck_missions_one_principal
            CHECK ((user_id IS NOT NULL)::int
                 + (service_account_id IS NOT NULL)::int = 1);
    END IF;
END $$;

ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS mission_id uuid REFERENCES missions(id);

CREATE INDEX IF NOT EXISTS idx_missions_session
    ON missions (session_id);
CREATE INDEX IF NOT EXISTS idx_tasks_mission
    ON tasks (mission_id);


-- ----------------------------------------------------------------------------
-- Scheduled sessions — relax single-principal ownership.  ``user_id`` was
-- NOT NULL with a FK to users(id) — only user JWTs could create /loop
-- schedules.  Sessions created through ops's Work UI authenticate as
-- per-user service accounts (the surogates session row has user_id=NULL,
-- service_account_id=<sa>), so they could never start a /loop.
--
-- Drop NOT NULL on user_id, add service_account_id, and enforce the
-- principal invariant (exactly one of the two is set) via CHECK.  The
-- table itself is created by ``Base.metadata.create_all`` (no fallback
-- CREATE TABLE here), so the only retrofit work is the column / index /
-- constraint reshape for existing PROD DBs.
-- ----------------------------------------------------------------------------

ALTER TABLE scheduled_sessions
    ALTER COLUMN user_id DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS service_account_id uuid
        REFERENCES service_accounts(id);

-- Who a run acts for and what it logs in as, captured when the schedule is
-- made; a run's own time limit.
ALTER TABLE scheduled_sessions
    ADD COLUMN IF NOT EXISTS context jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS max_run_seconds integer;

-- Replace the old (org, user, agent) index with one that covers both
-- principal shapes.  Old index is dropped explicitly because the name
-- is changing.
DROP INDEX IF EXISTS idx_scheduled_sessions_user;
CREATE INDEX IF NOT EXISTS idx_scheduled_sessions_principal
    ON scheduled_sessions (org_id, user_id, service_account_id, agent_id);

-- Exactly one principal must be set.  Wrapped in DO block so the ADD is
-- idempotent (PG has no ADD CONSTRAINT IF NOT EXISTS).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_scheduled_sessions_one_principal'
          AND conrelid = 'scheduled_sessions'::regclass
    ) THEN
        ALTER TABLE scheduled_sessions
            ADD CONSTRAINT ck_scheduled_sessions_one_principal
            CHECK ((user_id IS NOT NULL)::int
                 + (service_account_id IS NOT NULL)::int = 1);
    END IF;
END $$;

-- A schedule's cadence kind and parameters, and how it ended.
ALTER TABLE scheduled_sessions
    ADD COLUMN IF NOT EXISTS kind                 text,
    ADD COLUMN IF NOT EXISTS cadence              jsonb,
    ADD COLUMN IF NOT EXISTS paused_at            timestamptz,
    ADD COLUMN IF NOT EXISTS ended_reason         text,
    ADD COLUMN IF NOT EXISTS ended_by             text,
    ADD COLUMN IF NOT EXISTS consecutive_failures integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS ended_announced_at   timestamptz;

-- Rows made before ``kind`` keep their cadence in ``schedule``: a cron, or a
-- dynamic loop.  Every reader goes by ``kind`` alone.
UPDATE scheduled_sessions
SET kind = CASE WHEN schedule->>'kind' = 'dynamic_loop' THEN 'dynamic' ELSE 'cron' END,
    cadence = CASE WHEN schedule->>'kind' = 'dynamic_loop' THEN '{}'::jsonb
                   ELSE jsonb_build_object('cron', schedule->>'cron') END,
    timezone = COALESCE(schedule->>'timezone_name', timezone)
WHERE kind IS NULL;

-- A schedule's runs, looked up by the schedule they belong to.
CREATE INDEX IF NOT EXISTS idx_sessions_scheduled_session
    ON sessions ((config->>'scheduled_session_id'))
    WHERE channel = 'scheduled';

-- The ticker plans active rows with no next run (made or resumed by the
-- control plane); dynamic loops waiting on their run look the same, and
-- ``plan_unscheduled`` tells them apart.
CREATE INDEX IF NOT EXISTS idx_scheduled_sessions_unplanned
    ON scheduled_sessions (updated_at)
    WHERE status = 'active' AND next_run_at IS NULL;

-- Ended schedules are kept, so the per-tick sweeps over recent endings and
-- over open scheduled runs must not scan all of history.
CREATE INDEX IF NOT EXISTS idx_scheduled_sessions_cancelled
    ON scheduled_sessions (updated_at)
    WHERE status = 'cancelled';
CREATE INDEX IF NOT EXISTS idx_scheduled_sessions_unannounced
    ON scheduled_sessions (updated_at)
    WHERE ended_announced_at IS NULL AND status IN ('completed', 'failed');
CREATE INDEX IF NOT EXISTS idx_sessions_open_scheduled
    ON sessions (created_at)
    WHERE channel = 'scheduled' AND status IN ('active', 'paused');


-- ----------------------------------------------------------------------------
-- v_session_tree -- recursive ancestry of sessions via parent_id.
--
-- Each row has the session's ``root_session_id`` (the top-level ancestor),
-- its ``depth`` from the root, and an ``ancestor_path`` array of UUIDs from
-- root to self.  Use this for rendering expert-delegation sub-session trees
-- and for audit queries that walk an entire delegation subtree.
-- ----------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_session_tree AS
WITH RECURSIVE tree AS (
    SELECT
        s.id                AS session_id,
        s.id                AS root_session_id,
        s.parent_id,
        0::int              AS depth,
        ARRAY[s.id]::uuid[] AS ancestor_path,
        s.org_id,
        s.user_id,
        s.agent_id,
        s.channel,
        s.status,
        s.title,
        s.model,
        s.created_at,
        s.updated_at
    FROM sessions s
    WHERE s.parent_id IS NULL

    UNION ALL

    SELECT
        s.id,
        t.root_session_id,
        s.parent_id,
        t.depth + 1,
        t.ancestor_path || s.id,
        s.org_id,
        s.user_id,
        s.agent_id,
        s.channel,
        s.status,
        s.title,
        s.model,
        s.created_at,
        s.updated_at
    FROM sessions s
    JOIN tree t ON s.parent_id = t.session_id
)
SELECT * FROM tree;


-- ----------------------------------------------------------------------------
-- v_tool_invocations -- tool.call events joined with their matching
-- tool.result event.  One row per tool call.  ``result_event_id`` and
-- ``completed_at`` are NULL when the call has no recorded result yet
-- (interrupted, still running, or harness crashed mid-call).
-- ----------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_tool_invocations AS
SELECT
    call.id                                  AS call_event_id,
    call.session_id,
    call.org_id,
    call.user_id,
    s.agent_id,
    call.data->>'name'                       AS tool_name,
    call.data->>'tool_call_id'               AS tool_call_id,
    call.data->'arguments'                   AS arguments,
    call.created_at                          AS called_at,
    res.id                                   AS result_event_id,
    res.data->>'content'                     AS result_content,
    NULLIF(res.data->>'elapsed_ms', '')::bigint AS elapsed_ms,
    res.created_at                           AS completed_at
FROM events call
JOIN sessions s ON s.id = call.session_id
LEFT JOIN LATERAL (
    SELECT r.id, r.data, r.created_at
    FROM events r
    WHERE r.session_id = call.session_id
      AND r.type = 'tool.result'
      AND r.data->>'tool_call_id' = call.data->>'tool_call_id'
      AND r.id > call.id
    ORDER BY r.id ASC
    LIMIT 1
) res ON true
WHERE call.type = 'tool.call';


-- ----------------------------------------------------------------------------
-- v_tool_usage_daily -- daily rollup: tool calls per (org, user, agent, tool).
-- Drop-in source for dashboard queries like "top 10 tools per user last 7d".
-- ----------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_tool_usage_daily AS
SELECT
    e.org_id,
    e.user_id,
    s.agent_id,
    e.data->>'name'                 AS tool_name,
    date_trunc('day', e.created_at) AS day,
    COUNT(*)                        AS call_count
FROM events e
JOIN sessions s ON s.id = e.session_id
WHERE e.type = 'tool.call'
GROUP BY e.org_id, e.user_id, s.agent_id, e.data->>'name',
         date_trunc('day', e.created_at);


-- ----------------------------------------------------------------------------
-- v_policy_denials -- every policy.denied event with session context.
-- Feeds the "all denials last 7d" audit view and compliance reports.
-- ----------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_policy_denials AS
SELECT
    e.id              AS event_id,
    e.session_id,
    e.org_id,
    e.user_id,
    s.agent_id,
    s.channel,
    e.data->>'tool'   AS tool_name,
    e.data->>'reason' AS reason,
    e.created_at
FROM events e
JOIN sessions s ON s.id = e.session_id
WHERE e.type = 'policy.denied';


-- ----------------------------------------------------------------------------
-- v_expert_outcomes -- each expert.delegation joined with its nearest
-- expert.result/expert.failure and any subsequent user feedback
-- (expert.endorse / expert.override).
--
-- Drives two UI needs: "sessions with expert.override" (filter feedback_type)
-- and training-data quality signals (outcome_type + feedback_type together
-- tell you whether the expert's output was accepted).
-- ----------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_expert_outcomes AS
SELECT
    d.id                                          AS delegation_event_id,
    d.session_id,
    d.org_id,
    d.user_id,
    d.data->>'expert'                             AS expert_name,
    d.data->>'task'                               AS task,
    d.created_at                                  AS delegated_at,
    r.id                                          AS result_event_id,
    r.type                                        AS outcome_type,
    (r.data->>'success')::boolean                 AS success,
    NULLIF(r.data->>'iterations_used', '')::int   AS iterations_used,
    r.data->>'error'                              AS error,
    r.created_at                                  AS completed_at,
    fb.id                                         AS feedback_event_id,
    fb.type                                       AS feedback_type,
    fb.data->>'rating'                            AS feedback_rating,
    fb.data->>'reason'                            AS feedback_reason,
    -- `source` defaults to 'user' for rows predating the discriminator.
    COALESCE(fb.data->>'source', 'user')          AS feedback_source,
    fb.data->>'rated_by_user_id'                  AS rated_by_user_id,
    fb.data->>'rated_by_service_account_id'       AS rated_by_service_account_id,
    NULLIF(fb.data->>'score', '')::float          AS feedback_score,
    fb.data->'criteria'                           AS feedback_criteria,
    fb.data->>'rationale'                         AS feedback_rationale,
    fb.created_at                                 AS feedback_at
FROM events d
LEFT JOIN LATERAL (
    SELECT r.id, r.type, r.data, r.created_at
    FROM events r
    WHERE r.session_id = d.session_id
      AND r.type IN ('expert.result', 'expert.failure')
      AND r.data->>'expert' = d.data->>'expert'
      AND r.id > d.id
    ORDER BY r.id ASC
    LIMIT 1
) r ON true
LEFT JOIN LATERAL (
    SELECT fb.id, fb.type, fb.data, fb.created_at
    FROM events fb
    WHERE fb.session_id = d.session_id
      AND fb.type IN ('expert.endorse', 'expert.override')
      AND NULLIF(fb.data->>'target_event_id', '')::bigint = r.id
    ORDER BY fb.id ASC
    LIMIT 1
) fb ON true
WHERE d.type = 'expert.delegation';


-- ----------------------------------------------------------------------------
-- v_session_messages -- the message-shaped events in a session, in the
-- format training data exporters and chat-log renderers need.
--
-- Includes every event type that contributes to the conversation: user
-- messages, LLM responses, tool calls/results, expert delegation/outcome,
-- user feedback (both on expert output and on regular LLM turns), and what
-- the chat shows of its routines and helpers (loop results, a browser login
-- it could not use, a helper's progress).
-- Context-engineering events (context.compact, harness.wake, etc.) and
-- policy decision events are intentionally excluded; they have their own
-- dedicated views.
-- ----------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_session_messages AS
SELECT
    e.id         AS event_id,
    e.session_id,
    e.org_id,
    e.user_id,
    e.type,
    e.data,
    e.created_at,
    s.model,
    s.agent_id
FROM events e
JOIN sessions s ON s.id = e.session_id
WHERE e.type IN (
    'user.message',
    'skill.invoked',
    'llm.response',
    'tool.call',
    'tool.result',
    'expert.delegation',
    'expert.result',
    'expert.failure',
    'expert.endorse',
    'expert.override',
    'user.feedback',
    'loop.result',
    'browser.profile_unavailable',
    'delegation.progress'
);


-- ----------------------------------------------------------------------------
-- v_skill_trajectories -- one row per skill.invoked event with the
-- event-id range that makes up the user-ask -> assistant-answer
-- trajectory.  Feeds the bootstrap path for new experts: graduate a
-- prompt-based skill into a fine-tuned SLM by distilling the
-- trajectories of its `/<skill> args` invocations.
--
-- ``trajectory_end_event_id`` is the id of the first event that closes
-- the trajectory (next user.message, next skill.invoked, or a session
-- terminal event).  ``NULL`` means the trajectory runs to the end of
-- the session's event stream.  Trajectory content is the event range
-- ``skill_event_id < id < COALESCE(trajectory_end_event_id, +inf)``.
-- ----------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_skill_trajectories AS
SELECT
    si.id                              AS skill_event_id,
    si.session_id,
    si.org_id,
    si.user_id,
    s.agent_id,
    si.data->>'skill'                  AS skill,
    si.data->>'raw_message'            AS raw_message,
    si.data->>'staged_at'              AS staged_at,
    si.created_at                      AS invoked_at,
    boundary.id                        AS trajectory_end_event_id,
    boundary.type                      AS trajectory_end_type
FROM events si
JOIN sessions s ON s.id = si.session_id
LEFT JOIN LATERAL (
    SELECT b.id, b.type
    FROM events b
    WHERE b.session_id = si.session_id
      AND b.id > si.id
      AND b.type IN (
          'user.message',
          'skill.invoked',
          'session.complete',
          'session.fail'
      )
    ORDER BY b.id ASC
    LIMIT 1
) boundary ON true
WHERE si.type = 'skill.invoked';


-- ----------------------------------------------------------------------------
-- v_response_feedback -- each llm.response joined with its user.feedback
-- (if any).  Drives training-data selection for ordinary chat turns the
-- same way v_expert_outcomes drives expert training.
-- ----------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_response_feedback AS
SELECT
    r.id                                             AS response_event_id,
    r.session_id,
    r.org_id,
    r.user_id,
    s.agent_id,
    r.data->'message'->>'content'                    AS response_content,
    r.data->>'model'                                 AS model,
    r.created_at                                     AS responded_at,
    fb.id                                            AS feedback_event_id,
    fb.data->>'rating'                               AS feedback_rating,
    fb.data->>'reason'                               AS feedback_reason,
    fb.data->>'rated_by_user_id'                     AS rated_by_user_id,
    fb.data->>'rated_by_service_account_id'          AS rated_by_service_account_id,
    -- `source` defaults to 'user' for rows predating the discriminator.
    COALESCE(fb.data->>'source', 'user')             AS feedback_source,
    NULLIF(fb.data->>'score', '')::float             AS feedback_score,
    fb.data->'criteria'                              AS feedback_criteria,
    fb.data->>'rationale'                            AS feedback_rationale,
    fb.created_at                                    AS feedback_at
FROM events r
JOIN sessions s ON s.id = r.session_id
LEFT JOIN LATERAL (
    SELECT fb.id, fb.data, fb.created_at
    FROM events fb
    WHERE fb.session_id = r.session_id
      AND fb.type = 'user.feedback'
      AND NULLIF(fb.data->>'target_event_id', '')::bigint = r.id
    ORDER BY fb.id ASC
    LIMIT 1
) fb ON true
WHERE r.type = 'llm.response';


-- ----------------------------------------------------------------------------
-- v_training_candidates -- per-session summary with the quality signals a
-- training-data selector needs to decide whether to include the session.
--
-- A "good" training example is a completed session with no policy denials,
-- no expert overrides, and no harness crashes.  Consumers apply their own
-- thresholds on top of this view.
-- ----------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_training_candidates AS
SELECT
    s.id              AS session_id,
    s.org_id,
    s.user_id,
    s.agent_id,
    s.model,
    s.status,
    s.parent_id,
    s.created_at,
    s.updated_at,
    s.message_count,
    s.tool_call_count,
    s.input_tokens,
    s.output_tokens,
    s.estimated_cost_usd,
    EXISTS (
        SELECT 1 FROM events e
        WHERE e.session_id = s.id AND e.type = 'policy.denied'
    ) AS had_policy_denial,
    EXISTS (
        SELECT 1 FROM events e
        WHERE e.session_id = s.id AND e.type = 'expert.override'
    ) AS had_expert_override,
    EXISTS (
        SELECT 1 FROM events e
        WHERE e.session_id = s.id AND e.type = 'expert.endorse'
    ) AS had_expert_endorse,
    EXISTS (
        SELECT 1 FROM events e
        WHERE e.session_id = s.id AND e.type = 'harness.crash'
    ) AS had_crash,
    EXISTS (
        SELECT 1 FROM events e
        WHERE e.session_id = s.id AND e.type = 'saga.compensate'
    ) AS had_saga_compensation,
    EXISTS (
        SELECT 1 FROM events e
        WHERE e.session_id = s.id
          AND e.type = 'user.feedback'
          AND e.data->>'rating' = 'down'
    ) AS had_response_thumbs_down,
    EXISTS (
        SELECT 1 FROM events e
        WHERE e.session_id = s.id
          AND e.type = 'user.feedback'
          AND e.data->>'rating' = 'up'
    ) AS had_response_thumbs_up
FROM sessions s;

-- ── agent_users retrofits ────────────────────────────────────────────
-- Tombstone column for operator removals (create_all never alters
-- existing tables, so the retrofit lives here).
ALTER TABLE agent_users ADD COLUMN IF NOT EXISTS removed_at timestamp;

-- One account per (org, email): the login key and the Configure→Users
-- attach-by-email guard both assume it. Guarded — legacy databases may
-- already carry duplicates, and failing every migrate over old data
-- would be worse than the (rare) race the index closes; the NOTICE
-- makes the skip visible so the duplicates get cleaned deliberately.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM users
        GROUP BY org_id, lower(email) HAVING count(*) > 1
    ) THEN
        RAISE NOTICE 'skipping uq_users_org_lower_email: duplicate emails exist';
    ELSE
        CREATE UNIQUE INDEX IF NOT EXISTS uq_users_org_lower_email
            ON users (org_id, lower(email));
    END IF;
END $$;

-- ── users: self-registration profile fields ─────────────────────────
-- Full name lives in display_name; username is a per-org unique
-- lowercase handle, phone is optional free-form.
ALTER TABLE users ADD COLUMN IF NOT EXISTS username text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS phone text;
-- Most recent sign-in method (Firebase ``sign_in_provider`` / "password"
-- for local logins). Drives the web app's password-reset affordance.
ALTER TABLE users ADD COLUMN IF NOT EXISTS sign_in_provider text;
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_org_username
    ON users (org_id, lower(username))
    WHERE username IS NOT NULL;

-- ── program_invitations: one person's history in one Program ────────
-- The missed check-in streak and the silence pause read a person's last
-- few invitations of a Program, newest first, once per person per pass.
-- Without this every one of those reads scans the Program's whole history.
CREATE INDEX IF NOT EXISTS idx_program_invitations_program_user
    ON program_invitations (program_id, user_id, created_at);

-- Who opened a document in the editor, by editor user id.
ALTER TABLE office_sessions ADD COLUMN IF NOT EXISTS user_names JSONB NOT NULL DEFAULT '{}'::jsonb;

-- How many of the document server's history entries an open document's landed saves covered.
ALTER TABLE office_sessions ADD COLUMN IF NOT EXISTS changes_seen INTEGER NOT NULL DEFAULT 0;

-- The case whose document an editor row is, when a case's file was opened.
ALTER TABLE office_sessions ADD COLUMN IF NOT EXISTS case_id uuid;

-- ── followup_enrollments: who the person is, and how sure the start is ──
-- The user's display name is their WhatsApp profile, which is a relative's
-- when a relative enrolled them; these name the person and who answers.
ALTER TABLE followup_enrollments
    ADD COLUMN IF NOT EXISTS patient_name text,
    ADD COLUMN IF NOT EXISTS answered_by text,
    ADD COLUMN IF NOT EXISTS start_date_approximate boolean NOT NULL DEFAULT false;

-- ── followup_settings: how long a team member's held message may wait ─
ALTER TABLE followup_settings
    ADD COLUMN IF NOT EXISTS operator_unanswered_hours integer;

-- ── red alerts nobody opened are sent again, once ────────────────────
ALTER TABLE followup_settings
    ADD COLUMN IF NOT EXISTS red_alert_reminder_minutes integer;
ALTER TABLE outreach_messages
    ADD COLUMN IF NOT EXISTS severity text,
    ADD COLUMN IF NOT EXISTS reminded_at timestamptz;

-- ── a red alert's reminder, kept off the escalation it reminds of ────
ALTER TABLE program_invitations
    ADD COLUMN IF NOT EXISTS reminder_state text;

-- ── a message's outcome is reported once ─────────────────────────────
ALTER TABLE outreach_messages
    ADD COLUMN IF NOT EXISTS reported_at timestamptz;
ALTER TABLE program_invitations
    ADD COLUMN IF NOT EXISTS reported_at timestamptz;
CREATE INDEX IF NOT EXISTS idx_outreach_messages_unreported
    ON outreach_messages (created_at) WHERE reported_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_program_invitations_unreported
    ON program_invitations (created_at) WHERE reported_at IS NULL;

-- ── whether a team member read an alert ──────────────────────────────
ALTER TABLE outreach_messages
    ADD COLUMN IF NOT EXISTS read_at timestamptz;

-- ── a text's provider id, kept apart from its template's ─────────────
ALTER TABLE outreach_messages
    ADD COLUMN IF NOT EXISTS text_message_id text;
CREATE INDEX IF NOT EXISTS ix_outreach_messages_text_message_id
    ON outreach_messages (text_message_id);

-- ── "care team" became "team": rewrite what was recorded under the old name
UPDATE outreach_messages
    SET created_by = 'team:' || substr(created_by, length('care-team:') + 1)
    WHERE created_by LIKE 'care-team:%';
UPDATE followup_enrollments SET enrolled_by = 'team' WHERE enrolled_by = 'care_team';

-- ── what an alert is about, apart from how its text presents it ──────
ALTER TABLE outreach_messages
    ADD COLUMN IF NOT EXISTS alert_reason text;

-- ── journeys: a step picks a channel per person, and falls back ──────
ALTER TABLE program_invitations
    ADD COLUMN IF NOT EXISTS channel_identifier text,
    ADD COLUMN IF NOT EXISTS outcome text,
    ADD COLUMN IF NOT EXISTS fallback_platforms jsonb,
    ADD COLUMN IF NOT EXISTS attempts jsonb,
    ADD COLUMN IF NOT EXISTS read_at timestamptz,
    ADD COLUMN IF NOT EXISTS escalation_owner text,
    ADD COLUMN IF NOT EXISTS resolved_at timestamptz;
ALTER TABLE followup_enrollments
    ADD COLUMN IF NOT EXISTS timezone text,
    ADD COLUMN IF NOT EXISTS preferred_channel text;
ALTER TABLE followup_settings
    ADD COLUMN IF NOT EXISTS senders jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS team_channels jsonb NOT NULL DEFAULT '{}'::jsonb;
CREATE INDEX IF NOT EXISTS idx_program_invitations_person_open
    ON program_invitations (org_id, agent_id, user_id, response_state);

-- ── escalations: one record each, raised in a step or outside any ────
CREATE TABLE IF NOT EXISTS escalations (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL,
    agent_id text NOT NULL,
    user_id uuid NOT NULL,
    platform text,
    invitation_id uuid,
    program_id uuid,
    kind text NOT NULL,
    severity text,
    reason text NOT NULL,
    state text NOT NULL DEFAULT 'raised',
    session_id uuid,
    owner text,
    resolved_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_escalations_agent_open
    ON escalations (org_id, agent_id, resolved_at, created_at);
CREATE INDEX IF NOT EXISTS ix_escalations_invitation_id
    ON escalations (invitation_id);
-- Earlier escalations lived on their step's invitation, or (outside a
-- step) only as the team's alert messages; each becomes one record.  An
-- alert's key is escalation:<invitation or ->:<id>:<recipient>.
INSERT INTO escalations (id, org_id, agent_id, user_id, platform, invitation_id,
                         program_id, kind, severity, reason, state, session_id,
                         owner, resolved_at, created_at)
SELECT pi.id, pi.org_id, pi.agent_id, pi.user_id, pi.platform, pi.id, pi.program_id,
       CASE WHEN a.dedupe_key LIKE 'missed:%' OR coalesce(pi.reason, '') LIKE 'No answer to%'
            THEN 'missed_replies' ELSE 'alert' END,
       a.severity, coalesce(a.alert_reason, pi.reason, ''), pi.escalation_state,
       pi.session_id, pi.escalation_owner, pi.resolved_at, coalesce(a.created_at, pi.created_at)
FROM program_invitations pi
LEFT JOIN LATERAL (
    SELECT o.dedupe_key, o.severity, o.alert_reason, o.created_at
    FROM outreach_messages o
    WHERE o.org_id = pi.org_id AND o.kind = 'alert'
      AND (o.dedupe_key LIKE 'escalation:' || pi.id::text || ':%'
           OR o.dedupe_key LIKE 'missed:' || pi.id::text || ':%')
    ORDER BY o.created_at
    LIMIT 1
) a ON true
WHERE pi.escalation_state IN ('raised', 'failed')
  AND NOT EXISTS (SELECT 1 FROM escalations e WHERE e.invitation_id = pi.id)
ON CONFLICT (id) DO NOTHING;
INSERT INTO escalations (id, org_id, agent_id, user_id, platform, kind, severity,
                         reason, state, session_id, created_at)
SELECT DISTINCT ON (k.key)
       split_part(k.key, ':', 3)::uuid, o.org_id, o.agent_id, o.subject_user_id,
       s.channel, 'alert', o.severity, coalesce(o.alert_reason, o.text), 'raised',
       ev.session_id, o.created_at
FROM outreach_messages o
CROSS JOIN LATERAL (SELECT regexp_replace(o.dedupe_key, ':[^:]*$', '') AS key) k
LEFT JOIN LATERAL (
    SELECT e.session_id FROM events e
    WHERE e.org_id = o.org_id AND e.type = 'followup.escalation'
      AND e.data->>'escalation' = k.key
    LIMIT 1
) ev ON true
LEFT JOIN sessions s ON s.id = ev.session_id
WHERE o.kind = 'alert' AND o.dedupe_key LIKE 'escalation:-:%'
  AND o.subject_user_id IS NOT NULL
  AND split_part(k.key, ':', 3) ~ '^[0-9a-f]{32}$'
ORDER BY k.key, o.created_at
ON CONFLICT (id) DO NOTHING;

-- ── a step's fallbacks and attempts: none is SQL NULL, not JSON null ─
UPDATE program_invitations SET fallback_platforms = NULL WHERE fallback_platforms = 'null'::jsonb;
UPDATE program_invitations SET attempts = NULL WHERE attempts = 'null'::jsonb;

-- ── a step's outcome is the agent's label, never its status ──────────
UPDATE program_invitations SET outcome = NULL WHERE outcome IN ('completed', 'declined');

-- ── a browser push when a message waits in a person's web chat ───────
ALTER TABLE followup_settings
    ADD COLUMN IF NOT EXISTS web_push boolean NOT NULL DEFAULT false;

-- ── followed people and their contacts; the team's permissions ───────
ALTER TABLE program_invitations
    ADD COLUMN IF NOT EXISTS person_user_id uuid;
ALTER TABLE followup_enrollments
    ADD COLUMN IF NOT EXISTS memory_boundary text;
ALTER TABLE followup_settings
    ADD COLUMN IF NOT EXISTS team_permissions jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS team_destinations jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE outreach_messages
    ADD COLUMN IF NOT EXISTS destination text;
-- An escalation from a conversation with no known person is still queued.
ALTER TABLE escalations ALTER COLUMN user_id DROP NOT NULL;
CREATE INDEX IF NOT EXISTS idx_program_invitations_occurrence_person
    ON program_invitations (occurrence_id, person_user_id);
-- An enrollment a relative made on their own account for someone else
-- (answered_by set, the name not theirs) becomes the followed person's: a
-- user of their own, with the relative as a confirmed contact.  The ids are
-- derived from the enrollment, so a re-run finds what it made and changes
-- nothing; an enrollment already on a person with contacts is left alone.
DROP TABLE IF EXISTS pg_temp._followed_people;
CREATE TEMP TABLE _followed_people AS
SELECT e.id AS enrollment_id, e.org_id, e.agent_id, e.user_id AS contact_id, e.protocol,
       btrim(e.patient_name) AS name, btrim(e.answered_by) AS relation,
       md5('followed-person:' || e.org_id || ':' || e.agent_id || ':' || e.user_id
           || ':' || lower(btrim(e.patient_name)))::uuid AS person_id
FROM followup_enrollments e
JOIN users u ON u.id = e.user_id
WHERE nullif(btrim(e.answered_by), '') IS NOT NULL
  AND nullif(btrim(e.patient_name), '') IS NOT NULL
  AND lower(btrim(e.patient_name)) <> lower(btrim(u.display_name))
  AND NOT EXISTS (
      SELECT 1 FROM followup_contacts c
      WHERE c.org_id = e.org_id AND c.agent_id = e.agent_id AND c.person_user_id = e.user_id
  );
INSERT INTO users (id, org_id, email, display_name, auth_provider)
SELECT DISTINCT ON (person_id) person_id, org_id,
       'person-' || person_id || '@' || org_id || '.people.surogate.local', name, 'external'
FROM _followed_people
ON CONFLICT (id) DO NOTHING;
INSERT INTO agent_users (id, org_id, agent_id, user_id, source, created_at)
SELECT DISTINCT ON (person_id) md5('agent-user:' || person_id)::uuid, org_id, agent_id,
       person_id, 'manual', now()
FROM _followed_people
ON CONFLICT (org_id, agent_id, user_id) DO NOTHING;
INSERT INTO followup_contacts (id, org_id, agent_id, person_user_id, contact_user_id,
                               relation, status, created_by, created_at)
SELECT DISTINCT ON (person_id) md5('contact:' || person_id)::uuid, org_id, agent_id,
       person_id, contact_id, relation, 'confirmed', 'migration', now()
FROM _followed_people
ON CONFLICT ON CONSTRAINT uq_followup_contact DO NOTHING;
-- The relative's steps of that journey were about the person.
UPDATE program_invitations pi SET person_user_id = f.person_id
FROM _followed_people f, program_schedules ps
WHERE pi.org_id = f.org_id AND pi.agent_id = f.agent_id AND pi.user_id = f.contact_id
  AND pi.person_user_id IS NULL AND ps.program_id = pi.program_id
  AND ps.config->'audience'->>'protocol' = f.protocol;
-- So were the escalations raised in those steps, and, when the relative
-- follows nobody else and is not followed themselves, every other one.
UPDATE escalations x SET user_id = f.person_id
FROM _followed_people f
WHERE x.org_id = f.org_id AND x.agent_id = f.agent_id AND x.user_id = f.contact_id
  AND (
      x.program_id IN (
          SELECT ps.program_id FROM program_schedules ps
          WHERE ps.org_id = f.org_id AND ps.agent_id = f.agent_id
            AND ps.config->'audience'->>'protocol' = f.protocol
      )
      OR (
          (SELECT count(DISTINCT g.person_id) FROM _followed_people g
           WHERE g.org_id = f.org_id AND g.agent_id = f.agent_id AND g.contact_id = f.contact_id) = 1
          AND NOT EXISTS (
              SELECT 1 FROM followup_enrollments o
              WHERE o.org_id = f.org_id AND o.agent_id = f.agent_id AND o.user_id = f.contact_id
                AND o.id NOT IN (SELECT enrollment_id FROM _followed_people)
          )
      )
  );
UPDATE followup_enrollments e SET user_id = f.person_id
FROM _followed_people f
WHERE e.id = f.enrollment_id;
DROP TABLE _followed_people;

-- ── the agent writes each step: when it was opened, woken, and held ──
ALTER TABLE program_invitations
    ADD COLUMN IF NOT EXISTS opened_at timestamptz,
    ADD COLUMN IF NOT EXISTS woken_at timestamptz,
    ADD COLUMN IF NOT EXISTS deadline_woken_at timestamptz;
CREATE INDEX IF NOT EXISTS idx_program_invitations_woken
    ON program_invitations (session_id) WHERE woken_at IS NOT NULL AND outbox_id IS NULL;
ALTER TABLE outreach_messages
    ADD COLUMN IF NOT EXISTS not_before timestamptz;

-- ── someone removed from an agent is no longer followed by it ──
-- What the removal itself now does (surogates.followup.store.stop_following),
-- for bindings removed before it did.  Matches only what a removed binding
-- still leaves live, so it settles to a no-op; a re-added person's binding
-- has no removed_at and is never touched.
UPDATE followup_enrollments e
SET status = 'ended', status_reason = 'Removed from the agent', updated_at = now()
FROM agent_users au
WHERE au.org_id = e.org_id AND au.agent_id = e.agent_id AND au.user_id = e.user_id
  AND au.removed_at IS NOT NULL AND e.status <> 'ended';
-- Their open escalations leave the Inbox.  Only when there are any: the
-- item is found by its exact key (escalation:<step or ->:<id hex>), which
-- inbox_items has no index on, so the scan must not run on every migrate.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM escalations x
        JOIN agent_users au
          ON au.org_id = x.org_id AND au.agent_id = x.agent_id AND au.user_id = x.user_id
        WHERE au.removed_at IS NOT NULL AND x.resolved_at IS NULL
    ) THEN
        UPDATE inbox_items i
        SET status = 'acknowledged', responded_at = now(), updated_at = now()
        FROM escalations x, agent_users au
        WHERE au.org_id = x.org_id AND au.agent_id = x.agent_id AND au.user_id = x.user_id
          AND au.removed_at IS NOT NULL AND x.resolved_at IS NULL
          AND i.org_id = x.org_id AND i.status = 'pending'
          AND i.payload->>'escalation' = 'escalation:'
              || coalesce(x.invitation_id::text, '-') || ':' || replace(x.id::text, '-', '');
    END IF;
END $$;
UPDATE escalations x
SET resolved_at = now()
FROM agent_users au
WHERE au.org_id = x.org_id AND au.agent_id = x.agent_id AND au.user_id = x.user_id
  AND au.removed_at IS NOT NULL AND x.resolved_at IS NULL;
UPDATE outreach_messages o
SET state = 'replaced', reason = 'Removed from the agent'
FROM agent_users au
WHERE au.org_id = o.org_id AND au.agent_id = o.agent_id
  AND au.user_id IN (o.user_id, o.subject_user_id)
  AND au.removed_at IS NOT NULL AND o.state IN ('queued', 'awaiting_reply');
DELETE FROM followup_contacts c
USING agent_users au
WHERE au.org_id = c.org_id AND au.agent_id = c.agent_id
  AND au.user_id IN (c.person_user_id, c.contact_user_id)
  AND au.removed_at IS NOT NULL;

-- ── a resolved escalation's alerts are not held for delivery ──
-- Resolving retires them (surogates.followup.store.retire_escalation_alerts);
-- this gives up those held from before it did.  Bounded by the held rows
-- (idx_outreach_messages_state), each joined to its escalation by primary
-- key: the id is read from the dedupe key, escalation:<step>:<hex>:… and
-- reminder:<step>:<hex>:… or missed:<id>:…, cast only once it has the shape.
UPDATE outreach_messages o
SET state = 'replaced', reason = 'Escalation resolved before it was read'
FROM escalations x
WHERE o.state IN ('queued', 'awaiting_reply')
  AND o.kind = 'alert'
  AND x.id = CASE
      WHEN split_part(o.dedupe_key, ':', 1) IN ('escalation', 'reminder')
           AND split_part(o.dedupe_key, ':', 3) ~ '^[0-9a-f]{32}$'
          THEN split_part(o.dedupe_key, ':', 3)::uuid
      WHEN split_part(o.dedupe_key, ':', 1) = 'missed'
           AND split_part(o.dedupe_key, ':', 2) ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
          THEN split_part(o.dedupe_key, ':', 2)::uuid
  END
  AND x.org_id = o.org_id AND x.resolved_at IS NOT NULL;

-- An agent-owned browser profile used, without being bound, on these channels.
ALTER TABLE browser_profiles
    ADD COLUMN IF NOT EXISTS use_in_channels text[] NOT NULL DEFAULT '{}'::text[];

-- The stand-in users a rehearsal run creates; readers of real people skip them.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS rehearsal_run_id uuid;
CREATE INDEX IF NOT EXISTS ix_users_rehearsal_run_id ON users (rehearsal_run_id);

-- When the agent last showed a rehearsal person "typing…".
ALTER TABLE IF EXISTS rehearsal_people
    ADD COLUMN IF NOT EXISTS agent_typing_at timestamptz;
ALTER TABLE IF EXISTS rehearsal_runs
    ADD COLUMN IF NOT EXISTS templates jsonb NOT NULL DEFAULT '[]'::jsonb;
