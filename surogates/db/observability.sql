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
