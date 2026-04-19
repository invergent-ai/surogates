# SQL Views

Defined in
[`surogates/db/observability.sql`](../../surogates/db/observability.sql)
and applied at startup via
[`apply_observability_ddl`](../../surogates/db/engine.py).  Every view
is `CREATE OR REPLACE` + `DROP IF EXISTS` so re-running the DDL is
idempotent.

External consumers should prefer views over raw JSONB queries — adding
new keys to an event's JSONB payload never breaks a view-backed
query, because the view's column list stays fixed.

**A note on `user_id`.** The column is `NULL` for every event in an
API-channel session (sessions submitted via `POST /v1/api/prompts`,
owned by a service account instead of a user).  Dashboards that group
by `user_id` should also group by `channel` to avoid collapsing
every service-account session into a single "unknown user" bucket.

| View | Driven by | Purpose |
|---|---|---|
| [`v_session_tree`](#v_session_tree) | `sessions.parent_id` | Recursive ancestry for expert-delegation subtrees. |
| [`v_tool_invocations`](#v_tool_invocations) | `tool.call` ⨝ `tool.result` | One row per tool call with its paired result. |
| [`v_tool_usage_daily`](#v_tool_usage_daily) | `tool.call` | Daily rollup per `(org, user, agent, tool)`. |
| [`v_policy_denials`](#v_policy_denials) | `policy.denied` | Every denial with session context. |
| [`v_expert_outcomes`](#v_expert_outcomes) | `expert.delegation` ⨝ `expert.result`/`.failure` ⨝ feedback | Expert invocations with outcome + user rating. |
| [`v_response_feedback`](#v_response_feedback) | `llm.response` ⨝ `user.feedback` | Model turns with their thumbs. |
| [`v_skill_trajectories`](#v_skill_trajectories) | `skill.invoked` ⨝ next trajectory boundary | Per-invocation labeled trajectories for bootstrapping new experts. |
| [`v_session_messages`](#v_session_messages) | Chronological subset of `events` | Message-shaped events only (strips lifecycle). |
| [`v_training_candidates`](#v_training_candidates) | `sessions` + aggregate flags | Per-session quality signals for training data selection. |

---

## `v_session_tree`

Recursive CTE over `sessions.parent_id` — each row has the session's
top-level ancestor (`root_session_id`), its `depth` from the root, and
an `ancestor_path` UUID array from root to self.  Use this to render
expert-delegation sub-session trees and to walk an entire delegation
subtree in a single query.

| Column | Type |
|---|---|
| `session_id` | UUID |
| `root_session_id` | UUID |
| `parent_id` | UUID (nullable) |
| `depth` | int |
| `ancestor_path` | UUID[] |
| `org_id`, `user_id`, `agent_id`, `channel`, `status`, `title`, `model`, `created_at`, `updated_at` | from `sessions` |

```sql
-- All sub-sessions spawned under session X
SELECT session_id, depth
FROM v_session_tree
WHERE root_session_id = $1
ORDER BY ancestor_path;
```

---

## `v_tool_invocations`

Pairs each `tool.call` with its matching `tool.result` via a
`LEFT JOIN LATERAL ... LIMIT 1`.  `result_event_id` and `completed_at`
are `NULL` when the call has no recorded result (interrupted,
harness-crashed, still running).

| Column | Type |
|---|---|
| `call_event_id` | bigint |
| `session_id`, `org_id`, `user_id` | UUID |
| `agent_id` | text |
| `tool_name`, `tool_call_id` | text |
| `arguments` | jsonb |
| `called_at`, `completed_at` | timestamptz |
| `result_event_id` | bigint (nullable) |
| `result_content` | text (nullable) |
| `elapsed_ms` | bigint (nullable) |

```sql
-- Slowest tool calls this week
SELECT tool_name, elapsed_ms, session_id
FROM v_tool_invocations
WHERE org_id = $1
  AND called_at > now() - interval '7 days'
  AND elapsed_ms IS NOT NULL
ORDER BY elapsed_ms DESC
LIMIT 20;
```

---

## `v_tool_usage_daily`

Daily rollup of tool calls.  Drop-in source for dashboards that need
"top tools per user" or "tool mix over time".

| Column | Type |
|---|---|
| `org_id`, `user_id` | UUID |
| `agent_id`, `tool_name` | text |
| `day` | timestamptz (day-truncated) |
| `call_count` | bigint |

```sql
-- Top 10 tools for user Y last 7 days
SELECT tool_name, SUM(call_count) AS calls
FROM v_tool_usage_daily
WHERE user_id = $1
  AND day > now() - interval '7 days'
GROUP BY tool_name
ORDER BY calls DESC
LIMIT 10;
```

---

## `v_policy_denials`

Every `policy.denied` event with session context (agent, channel).
Feeds the "all denials last 7d" audit view and compliance reports.

| Column | Type |
|---|---|
| `event_id` | bigint |
| `session_id`, `org_id`, `user_id` | UUID |
| `agent_id`, `channel`, `tool_name`, `reason` | text |
| `created_at` | timestamptz |

```sql
SELECT tool_name, reason, session_id, created_at
FROM v_policy_denials
WHERE org_id = $1
  AND created_at > now() - interval '7 days'
ORDER BY created_at DESC;
```

---

## `v_expert_outcomes`

Each `expert.delegation` joined with its nearest subsequent
`expert.result` / `expert.failure` and any user feedback
(`expert.endorse` / `expert.override`) on that result.  Drives two UI
needs:

- "Sessions where the user overrode the expert" (filter
  `feedback_type = 'expert.override'`).
- Training-data quality signals — `outcome_type` and `feedback_type`
  together tell you whether the expert's output was accepted.

| Column | Type |
|---|---|
| `delegation_event_id` | bigint |
| `session_id`, `org_id`, `user_id` | UUID |
| `expert_name`, `task` | text |
| `delegated_at` | timestamptz |
| `result_event_id` | bigint (nullable) |
| `outcome_type` | text (`expert.result` or `expert.failure`, nullable) |
| `success` | bool (nullable) |
| `iterations_used` | int (nullable) |
| `error` | text (nullable) |
| `completed_at`, `feedback_at` | timestamptz (nullable) |
| `feedback_event_id` | bigint (nullable) |
| `feedback_type` | text (`expert.endorse` or `expert.override`, nullable) |
| `feedback_rating`, `feedback_reason` | text (nullable) |

```sql
-- Experts whose last 50 invocations were most often overridden
SELECT expert_name,
       COUNT(*) FILTER (WHERE feedback_type = 'expert.override') AS overrides,
       COUNT(*) AS total
FROM v_expert_outcomes
WHERE org_id = $1
GROUP BY expert_name
HAVING COUNT(*) >= 50
ORDER BY overrides::float / total DESC;
```

---

## `v_response_feedback`

Each `llm.response` joined with its `user.feedback` (if any).  Drives
training-data selection for ordinary chat turns the same way
`v_expert_outcomes` drives expert training.

| Column | Type |
|---|---|
| `response_event_id` | bigint |
| `session_id`, `org_id`, `user_id` | UUID |
| `agent_id`, `response_content`, `model` | text |
| `responded_at`, `feedback_at` | timestamptz |
| `feedback_event_id` | bigint (nullable) |
| `feedback_rating`, `feedback_reason`, `rated_by_user_id` | text (nullable) |

```sql
-- Responses the user thumbs-downed last week
SELECT response_event_id, response_content, feedback_reason
FROM v_response_feedback
WHERE org_id = $1
  AND feedback_rating = 'down'
  AND feedback_at > now() - interval '7 days';
```

---

## `v_skill_trajectories`

One row per `skill.invoked` event with the id range of its trajectory
— the user's `/<skill> args` message, the `skill.invoked` marker, and
the assistant turns + tool exchanges that answered it.  Feeds the
**bootstrap path** for new experts: graduate a prompt-based skill into
a fine-tuned SLM by distilling every labeled invocation of that skill.
The skill is the class label.

`trajectory_end_event_id` is the id of the first event that closes the
trajectory — next `user.message`, next `skill.invoked`, or a session
terminal event (`session.complete` / `session.fail`).  `NULL` means the
trajectory runs to the end of the session's event stream (session
still active, or answered-but-never-followed-up).

| Column | Type |
|---|---|
| `skill_event_id` | bigint |
| `session_id`, `org_id`, `user_id` | UUID |
| `agent_id`, `skill` | text |
| `raw_message` | text |
| `staged_at` | text (nullable) |
| `invoked_at` | timestamptz |
| `trajectory_end_event_id` | bigint (nullable) |
| `trajectory_end_type` | text (nullable) |

```sql
-- All trajectories of skill X that landed a full reply (not cut short
-- by an immediate follow-up user message).  Drop tainted sessions.
SELECT st.skill_event_id, st.session_id, st.raw_message
FROM v_skill_trajectories st
JOIN v_training_candidates tc USING (session_id)
WHERE st.org_id = $1
  AND st.skill = $2
  AND NOT tc.had_policy_denial
  AND NOT tc.had_crash
  AND NOT tc.had_expert_override
  AND tc.status = 'completed';
```

Prefer `TrainingDataCollector.collect_for_skill(...)` in
[`surogates/jobs/training_collector.py`](../../surogates/jobs/training_collector.py)
over hand-built SQL — it walks the trajectory for each row in this
view, shapes the events into OpenAI fine-tuning JSONL
(`{role, content, tool_calls?}`), and handles the quality filters.

---

## `v_session_messages`

The message-shaped subset of `events` — user messages, skill
invocations, LLM responses, tool calls/results, expert
delegation/outcome, user feedback.  Context-engineering events
(`context.compact`, `harness.wake`, `session.*`) and governance
decisions are intentionally excluded; they have their own dedicated
views.

`skill.invoked` rows are surfaced here as inline markers between the
user's `/<skill> args...` message and the LLM response, the same way
`expert.delegation` is surfaced for `consult_expert`.  The original
`/<skill>` text remains in the preceding `user.message`.

Training-data exporters and chat-log renderers read this view to get
the events that matter for reconstructing the conversation.

| Column | Type |
|---|---|
| `event_id` | bigint |
| `session_id`, `org_id`, `user_id` | UUID |
| `type` | text |
| `data` | jsonb |
| `created_at` | timestamptz |
| `model`, `agent_id` | text |

```sql
-- Reconstruct a session's conversation in order
SELECT type, data
FROM v_session_messages
WHERE session_id = $1
ORDER BY event_id;
```

---

## `v_training_candidates`

Per-session summary with the quality flags a training-data selector
needs.  A "clean" training example is a completed session with no
policy denials, no expert overrides, and no harness crashes.
Consumers apply their own thresholds on top of this view.

| Column | Type |
|---|---|
| `session_id`, `org_id`, `user_id`, `parent_id` | UUID |
| `agent_id`, `model`, `status` | text |
| `created_at`, `updated_at` | timestamptz |
| `message_count`, `tool_call_count` | int |
| `input_tokens`, `output_tokens` | bigint |
| `estimated_cost_usd` | numeric |
| `had_policy_denial` | bool |
| `had_expert_override`, `had_expert_endorse` | bool |
| `had_crash` | bool |
| `had_saga_compensation` | bool |
| `had_response_thumbs_up`, `had_response_thumbs_down` | bool |

```sql
-- Clean sessions that landed a thumbs-up, ready for training export
SELECT session_id, agent_id, message_count
FROM v_training_candidates
WHERE org_id = $1
  AND status = 'completed'
  AND NOT had_policy_denial
  AND NOT had_expert_override
  AND NOT had_crash
  AND had_response_thumbs_up;
```
