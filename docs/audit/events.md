# Session Event Log (`events`)

The `events` table is the append-only source of truth for every action
inside a session — conversation turns, tool calls, policy decisions,
saga steps, expert delegations, user feedback.  One row per action,
one session per row.  For events that happen *outside* any session
(auth, MCP scan, credential access) see
[audit_log.md](audit_log.md).

## Table shape

```sql
CREATE TABLE events (
    id          BIGSERIAL PRIMARY KEY,
    session_id  UUID NOT NULL REFERENCES sessions(id),
    org_id      UUID REFERENCES orgs(id),    -- denormalized by trigger
    user_id     UUID REFERENCES users(id),   -- denormalized by trigger
    type        TEXT NOT NULL,               -- see "Event types" below
    data        JSONB,
    trace_id    TEXT,
    span_id     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`org_id` and `user_id` are populated automatically by the
`events_populate_tenant` trigger when a row is inserted — emit sites
never set them explicitly.  Queries that filter by tenant should hit
`events.org_id` directly rather than joining `sessions`.

### Indexes for audit queries

| Index | Use case |
|---|---|
| `(session_id, id)` | Chronological event read within a session (timeline). |
| `(session_id, type)` | Filter events of one type inside a session. |
| `(org_id, type, created_at)` | "All `policy.denied` last 7 days for org X." |
| `(org_id, user_id, type, created_at)` | "Top tools for user Y this month." |
| `(trace_id)` | Cross-session trace lookup. |

## Event types

Authoritative list: [`surogates/session/events.py`](../../surogates/session/events.py)
(`EventType` enum).  The sections below document the stable JSONB
shape of each.

### User interaction

`user.message` — a user-supplied turn.

| key | type | notes |
|---|---|---|
| `content` | string | Plain-text message body. |
| `media_urls` | string[] | Optional attachments (resolved URLs). |
| `media_types` | string[] | MIME types parallel to `media_urls`. |
| `source` | object | Channel origin: `{platform, chat_id, user_id, ...}`. |
| `message_type` | string | Channel-native type (`text`, `audio`, `photo`…). |
| `platform_message_id` | string | Provider-assigned message id, if any. |

### LLM interaction

`llm.request` — prompt sent to the model (pre-response, for audit).

`llm.response` — a model response (assistant turn).  The core event
that message replay, training export, and cost tracking read.

| key | type | notes |
|---|---|---|
| `message` | object | OpenAI-compatible assistant message: `{role, content, tool_calls?}`. |
| `model` | string | Model id the response came from. |
| `input_tokens` | int | Input tokens for this request. |
| `output_tokens` | int | Output tokens for this response. |
| `finish_reason` | string | `stop`, `length`, `tool_calls`, `budget_exhausted`, … |
| `cost_usd` | number | Estimated cost of this call. |

`llm.thinking` — extracted reasoning/thinking block (provider-dependent).

`llm.delta` — a streaming token delta (optional, emitted only when
streaming relay is enabled for the channel).

### Tool execution

`tool.call` — the harness is about to dispatch a tool.

| key | type | notes |
|---|---|---|
| `tool_call_id` | string | Provider id pairing with `tool.result`. |
| `name` | string | Tool name (e.g. `terminal`, `read_file`). |
| `arguments` | object | Sanitized args (workspace paths replaced with `__WORKSPACE__`). |
| `checkpoint_hash` | string | Optional filesystem snapshot hash for reversible ops. |

`tool.result` — outcome of a `tool.call`.

| key | type | notes |
|---|---|---|
| `tool_call_id` | string | Matches the paired `tool.call`. |
| `name` | string | Tool name. |
| `content` | string | Result body (may be JSON-encoded). |
| `elapsed_ms` | int | Wall-clock duration in milliseconds. |

### Governance

`policy.denied` — `GovernanceGate` blocked a tool call.

| key | type | notes |
|---|---|---|
| `tool` | string | Tool name that was blocked. |
| `reason` | string | Human-readable explanation. |
| `timestamp` | number | Emitter-side Unix epoch seconds. |

`policy.allowed` — `GovernanceGate` approved a tool call.  Off by
default because each `tool.call` is already an implicit allow; enable
`governance.log_allowed: true` in `config.yaml` (or
`SUROGATES_GOVERNANCE_LOG_ALLOWED=true`) for a complete decision trail
in compliance audits.

| key | type | notes |
|---|---|---|
| `tool` | string | Tool name that passed the check. |
| `check` | string | Which check ran (e.g. `workspace_sandbox`). |
| `timestamp` | number | Emitter-side Unix epoch seconds. |

### Sandbox lifecycle

`sandbox.provision`, `sandbox.execute`, `sandbox.result`,
`sandbox.destroy` — each carries at minimum `{sandbox_id}` and
backend-specific diagnostics (pod name, phase, etc.).

### Session lifecycle

`session.start`, `session.pause`, `session.resume`, `session.complete`,
`session.fail`, `session.reset` — coarse state transitions.
`session.reset` carries `{reason}`.  `session.fail` typically carries
`{error}`.

### Harness lifecycle

`harness.wake` — worker acquired the lease and started processing.

`harness.crash` — worker failed mid-session.

| key | type | notes |
|---|---|---|
| `error` | string | Exception repr. |

### Context management

`context.compact` — the harness compressed the message history.

| key | type | notes |
|---|---|---|
| `compacted_messages` | object[] | Post-compression message list. |
| `strategy` | string | Compression strategy used. |

`memory.update` — `MEMORY.md` or `USER.md` was mutated by the agent.

### Skill invocation

`skill.invoked` — the harness eagerly expanded a `/<skill> args...` user
message by calling `skill_view` server-side and inlining the skill body
into the message the LLM sees.  The original `/<skill> args...` text
remains in the preceding `user.message` event; this row records which
skill was resolved and where its supporting files were staged.  Emitted
at most once per user message — crash-recovery wakes that re-expand the
same message do not re-emit.

| key | type | notes |
|---|---|---|
| `skill` | string | Resolved skill name. |
| `raw_message` | string | Verbatim user text (e.g. `/arxiv cuda training llm 2026`). |
| `staged_at` | string \| null | Sandbox path where supporting files were staged, or `null` when the skill has no assets / dev path. |

### Expert delegation

`expert.delegation` — base LLM called `consult_expert`.

| key | type | notes |
|---|---|---|
| `expert` | string | Expert skill name. |
| `task` | string | Delegated subtask (truncated to 500 chars). |
| `tools` | string[] | Tools the expert can use. |
| `max_iterations` | int | Budget for the expert mini-loop. |

`expert.result` — expert completed successfully.

| key | type | notes |
|---|---|---|
| `expert` | string | |
| `success` | bool | Always `true` for this type. |
| `iterations_used` | int | |

`expert.failure` — expert hit budget, errored, or otherwise gave up.

| key | type | notes |
|---|---|---|
| `expert` | string | |
| `success` | bool | Always `false`. |
| `iterations_used` | int | |
| `error` | string | Optional failure reason. |

`expert.endorse` / `expert.override` — user feedback on an
`expert.result`.

| key | type | notes |
|---|---|---|
| `expert` | string | |
| `target_event_id` | int | `events.id` of the rated `expert.result`. |
| `rating` | string | `"up"` (endorse) or `"down"` (override). |
| `rated_by_user_id` | string | Author of the feedback. |
| `reason` | string | Optional free-text reason. |

### User feedback

`user.feedback` — user thumbs on a regular `llm.response` (not an
expert output; experts use `expert.endorse`/`expert.override`).  Used
by the training-data selector to prioritize or exclude trajectories.

| key | type | notes |
|---|---|---|
| `target_event_id` | int | `events.id` of the rated `llm.response`. |
| `rating` | string | `"up"` or `"down"`. |
| `rated_by_user_id` | string | Author of the feedback. |
| `reason` | string | Optional free-text reason. |

### Worker coordination

`worker.spawned`, `worker.complete`, `worker.failed` — used by the
coordinator pattern to track spawned sub-agents.

### Outcome / `/goal` loop

`user.define_outcome` — programmatic equivalent of `/goal`; defines an
outcome via the events API.

| key | type | notes |
|---|---|---|
| `description` | string | Outcome text. |
| `rubric` | object | Optional `{type, content}` block with evaluation criteria. |
| `max_iterations` | int | Optional override for `outcomes.max_iterations`. |

`outcome.defined` — outcome state was saved on the session.

| key | type | notes |
|---|---|---|
| `outcome_id` | string | Internal id (`outc_<hex>`). |
| `description` | string | Outcome text. |
| `rubric` | string | Resolved rubric text (default rubric when none was supplied). |
| `max_iterations` | int | Iteration budget for this outcome. |

`span.outcome_evaluation_start`, `span.outcome_evaluation_ongoing`,
`span.outcome_evaluation_end` — evaluator span across one turn.

| key | type | notes |
|---|---|---|
| `outcome_id` | string | |
| `iteration` | int | 1-based iteration counter. |
| `response_event_id` | int | `events.id` of the `llm.response` being graded (start only). |
| `outcome_evaluation_start_id` | int | Back-reference to the matching start event (end only). |
| `result` | string | `satisfied`, `needs_revision`, `blocked`, or `failed` (end only). |
| `explanation` | string | One-sentence rationale from the evaluator (end only). |
| `feedback` | string | Revision guidance the agent will see on the next continuation (end only). |
| `parse_failed` | bool | True when the evaluator output couldn't be parsed; flipping repeatedly auto-pauses the outcome (end only). |

`outcome.continuation` — Surogates queued another attempt; the
synthetic `user.message` that follows carries `synthetic:
outcome_continuation` and the model-visible continuation prompt.

| key | type | notes |
|---|---|---|
| `outcome_id` | string | |
| `iteration` | int | |
| `status_event_id` | int \| null | `events.id` of the assistant status message that preceded this continuation. |

`outcome.paused` — outcome paused by the user (`/goal pause`) or by
repeated evaluator parse failures.

| key | type | notes |
|---|---|---|
| `outcome_id` | string | |
| `reason` | string | `user-paused` or a specific evaluator-failure reason. |

`outcome.cleared` — outcome state was removed (`/goal clear`).

| key | type | notes |
|---|---|---|
| `outcome_id` | string \| null | Null when no outcome was active at clear time. |

### Sub-agent delegation

`delegation.start`, `delegation.complete`, `delegation.failed`,
`delegation.stale` — emitted on the **parent**'s event log by the
`delegate_task` tool. The child's own event log is still the source of
truth for everything that happened inside it; these events are the
parent-side audit trail.

`delegation.start` — a child session has been created and enqueued.

| key | type | notes |
|---|---|---|
| `child_session_id` | string | UUID of the new child session. |
| `goal` | string | Child's goal text. |
| `role` | string | `leaf` or `orchestrator`. |
| `depth` | int | Child's `delegation_depth` (root coordinator is `0`). |
| `agent_type` | string \| null | Resolved sub-agent name, if specified. |
| `model` | string \| null | Resolved model override, if any. |

`delegation.complete` — child finished successfully.

| key | type | notes |
|---|---|---|
| `child_session_id` | string | |
| `goal` | string | |
| `duration_seconds` | number | Wall-clock from spawn to completion. |
| `tool_call_count` | int | Number of `tool.call` events in the child. |
| `trace` | object[] | Each entry: `{name, ok, tool_call_id}`. `ok=false` means the matching `tool.result` had an error or never arrived. |
| `files_written` | string[] | `path` arguments from `write_file` / `patch` (replace mode) calls in the child, deduplicated. |
| `files_read` | string[] | `path` arguments from `read_file` calls in the child, deduplicated. |

`delegation.failed` — child errored, timed out, or hit `session.fail`.

| key | type | notes |
|---|---|---|
| `child_session_id` | string | |
| `goal` | string | |
| `reason` | string | Failure description. |
| `duration_seconds` | number | |

`delegation.stale` — one-shot per child, fired when the child stops
emitting events for longer than the configured threshold during poll.

| key | type | notes |
|---|---|---|
| `child_session_id` | string | |
| `idle_seconds` | number | Time since the last child event. |
| `in_tool` | bool | True when the child's last event is an unmatched `tool.call` (a tool is still running). |
| `threshold_seconds` | number | The threshold that was crossed (60 s idle, 180 s in-tool). |

### Saga orchestration

`saga.start` — multi-step tool chain began.

| key | type | notes |
|---|---|---|
| `saga_id` | string | Identifier for the saga instance. |
| `session_id` | string | Session that owns the saga. |
| `timestamp` | number | Unix epoch seconds. |

`saga.step_begin`, `saga.step_committed`, `saga.step_failed` —
per-step state transitions.

| key | type | notes |
|---|---|---|
| `saga_id` | string | |
| `step_id` | string | |
| `tool_name` | string | |
| `state` | string | State machine label. |
| `tool_call_id` | string | Optional, pairs with `tool.call` / `tool.result`. |
| `arguments` | object | Tool arguments for the step. |
| `compensation_tool` | string | Optional reverse-op tool name. |
| `compensation_args` | object | Optional reverse-op args. |
| `checkpoint_hash` | string | Filesystem snapshot for rollback. |
| `result` | any | Step output (only on `step_committed`). |
| `error` | string | Only on `step_failed`. |

`saga.compensate` — rollback in progress.

| key | type | notes |
|---|---|---|
| `saga_id` | string | |
| `steps_rolled_back` | int | |
| `reason` | string | Why compensation triggered. |
| `failed_steps` | string[] | Optional list of step ids whose compensation failed. |

`saga.complete` — terminal state.

| key | type | notes |
|---|---|---|
| `saga_id` | string | |
| `status` | string | `committed` or `compensated`. |
| `steps_executed` | int | |

## Views over `events`

[views.md](views.md) documents the SQL views in
[`surogates/db/observability.sql`](../../surogates/db/observability.sql)
that project these JSONB payloads into typed columns.  External
consumers should prefer views over raw JSONB queries — added keys do
not break view-based queries.
