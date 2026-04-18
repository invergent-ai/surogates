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
