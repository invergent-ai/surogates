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

CREATE INDEX IF NOT EXISTS idx_events_audit_type_time
    ON events (org_id, type, created_at);

CREATE INDEX IF NOT EXISTS idx_events_audit_user_time
    ON events (org_id, user_id, type, created_at);

CREATE INDEX IF NOT EXISTS idx_events_session_type
    ON events (session_id, type);

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
-- Agent (a different database — logical reference, not a FK).  The partial
-- unique index keeps it one service account per agent.  Retrofit for existing
-- databases; ``create_all`` covers fresh ones.
ALTER TABLE service_accounts
    ADD COLUMN IF NOT EXISTS agent_id text;

CREATE UNIQUE INDEX IF NOT EXISTS uq_service_accounts_agent
    ON service_accounts (agent_id)
    WHERE agent_id IS NOT NULL;

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
        REFERENCES service_accounts(id);

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
-- messages, LLM responses, tool calls/results, expert delegation/outcome
-- and user feedback (both on expert output and on regular LLM turns).
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
    'user.feedback'
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
