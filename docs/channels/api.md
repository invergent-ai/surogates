# API Channel

The API channel is a programmatic, fire-and-forget interface for non-interactive clients -- synthetic data generation pipelines, batch evaluation jobs, and any other workload that submits prompts from outside the web or messaging channels. Authentication is by org-scoped API key ("service-account token"); no user identity is involved.

The API channel is not a chat interface -- it accepts a prompt, creates a session, queues it for the worker, and returns the session identifier. Results are read directly from the `events` and `sessions` database tables.

## When to use it

| Use case | Example |
|---|---|
| Synthetic training-data generation | A pipeline iterates over dataset rows, submits each prompt as a session, and later sweeps the `events` table for `llm.response` rows to harvest completions. |
| Automated evaluations | A scorer submits thousands of prompts in parallel and reads `events.data` for downstream metrics. |
| Scheduled bulk work | A cron job dispatches org-wide prompt runs. |

Do **not** use the API channel for interactive experiences -- use the [web channel](web.md) instead, which streams tokens and tool calls live over SSE.

## Authentication

The client presents an API key in the `Authorization: Bearer` header. API keys have the prefix `surg_sk_` and are issued to an org by an admin:

```
POST /v1/admin/service-accounts
Authorization: Bearer <admin-jwt>

{
  "org_id": "00000000-...",
  "name": "dataset-gen-v1"
}
```

The raw token is returned **exactly once** in the response body (`token`). Store it immediately -- the server keeps only a SHA-256 hash and cannot recover the plaintext. List and revoke endpoints live under the same `/v1/admin/service-accounts` prefix.

API keys may only authenticate requests to routes under `/v1/api/*`. Presenting one anywhere else returns 403. Conversely, the `/v1/api/*` routes reject interactive JWTs so the two principal types stay cleanly separated.

## Submitting a prompt

```
POST /v1/api/prompts
Authorization: Bearer surg_sk_...

{
  "prompt": "Write a haiku about distributed systems.",
  "idempotency_key": "dataset-42/row-1337",
  "metadata": {
    "dataset_id": "ds_123",
    "row_index": 1337,
    "experiment": "baseline-v3"
  }
}
```

Response (`202 Accepted`):

```json
{
  "session_id": "8f...",
  "event_id": 42,
  "deduplicated": false
}
```

The worker picks the session off the Redis queue and processes it asynchronously. The pipeline owns the returned `session_id` and uses it to read results from the database.

### Idempotency

`idempotency_key` is an optional client-supplied string scoped per org. Two requests from the same org with the same key resolve to the **same** session:

- first call -> `deduplicated: false`, new session created
- second call -> `deduplicated: true`, original `session_id` returned, no new work queued

Use this to make pipeline retries safe under timeouts or restarts. Keys from different orgs do not collide.

### Metadata passthrough

Anything in `metadata` is persisted onto `sessions.config['pipeline_metadata']`. The pipeline joins results back to its source dataset by querying for sessions with specific metadata values -- no side-table required.

## Submitting a batch

```
POST /v1/api/prompts:batch
Authorization: Bearer surg_sk_...

{
  "prompts": [
    {"prompt": "...", "idempotency_key": "row-1", "metadata": {"i": 1}},
    {"prompt": "...", "idempotency_key": "row-2", "metadata": {"i": 2}}
  ]
}
```

Each entry is accepted independently. The response preserves input order so callers can zip results back to their input rows. Up to 100 prompts per request.

## Reading results

Each submitted prompt becomes a session (`channel='api'`). The pipeline reads:

| Signal | Source |
|---|---|
| Final LLM answer | `events` rows with `type = 'llm.response'` for the session |
| Tool calls / tool results | `events` rows with `type IN ('tool.call', 'tool.result')` |
| Completion status | `sessions.status` (`active`, `idle`, `completed`, `failed`) |
| Cost / token usage | `sessions.input_tokens`, `sessions.output_tokens`, `sessions.estimated_cost_usd` |
| Pipeline metadata | `sessions.config->'pipeline_metadata'` |

The `v_session_messages` view returns conversation-shaped events in training-data format; the `v_response_feedback` and `v_tool_invocations` views expose related signals. See [docs/audit/views.md](../audit/views.md) for the full catalog.

## Recording judge feedback

Pipelines that run an automated judge over their outputs record the judge's grade by `POST /v1/api/sessions/{session_id}/events/{event_id}/feedback`, authenticated with the same service-account token. The endpoint accepts binary `rating` (required), a numeric `score`, per-axis `criteria`, and a free-form `rationale`. The stored event carries `source: "judge"` so downstream training-data selection can weight judge feedback independently from human thumbs. See [Appendix B: Feedback (API Channel)](../appendices/api-reference.md#feedback-api-channel) for the full schema and idempotency semantics.

## Interaction with other subsystems

- **Training data**: API sessions participate in `TrainingDataCollector` exports on the same footing as every other channel -- successful expert delegations and skill invocations from pipeline-submitted prompts are eligible for fine-tuning.
- **Idle reset**: the session-reset CronJob resets API sessions in place without running the memory-flush agent -- service accounts have no per-user memory.
- **Memory**: API sessions use the org-shared memory directory, not user-scoped memory.
- **Permissions**: API keys carry no permissions; access is scoped entirely by org membership. They cannot reach admin, auth, or any other `/v1/` routes.
