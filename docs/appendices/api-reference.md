# Appendix B: REST API Reference

The REST API serves two roles: the web channel interface (browser SPA talks directly to these endpoints) and the internal API consumed by workers and channel adapters.

Base URL: `/v1`

All endpoints require authentication unless noted otherwise. Two token types are accepted:

- **JWT access tokens** (`Authorization: Bearer eyJ...`) -- for interactive users. Required on everything except `/v1/api/*`.
- **Service-account tokens** (`Authorization: Bearer surg_sk_...`) -- for programmatic clients. Accepted **only** on `/v1/api/*`; refused elsewhere. See [Service-Account Admin CRUD](#service-accounts-admin).

## Auth Endpoints

### `POST /v1/auth/login`

Authenticate and receive tokens.

**Request:**
```json
{
  "provider": "database",
  "credentials": {
    "email": "user@acme.com",
    "password": "..."
  }
}
```

Credentials: `{"email": "...", "password": "..."}`

**Response (200):**
```json
{
  "access_token": "eyJ...",
  "refresh_token": "eyJ...",
  "token_type": "bearer",
  "expires_in": 1800
}
```

### `POST /v1/auth/refresh`

Refresh an expired access token.

**Request:**
```json
{
  "refresh_token": "eyJ..."
}
```

**Response (200):**
```json
{
  "access_token": "eyJ...",
  "token_type": "bearer",
  "expires_in": 1800
}
```

### `POST /v1/auth/logout`

Invalidate the current session tokens.

### `GET /v1/auth/providers`

List configured auth providers for the org (used by the login UI to show available options).

**Response (200):**
```json
{
  "providers": [
    {"type": "database", "name": "Email & Password"},
    {"type": "database", "name": "Email & Password"}
  ]
}
```

## Sessions

### `POST /v1/sessions`

Create a new session.

**Request:**
```json
{
  "model": "claude-sonnet-4-20250514",
  "system": "You are a helpful assistant",
  "tools": ["terminal", "web_search"],
  "workspace": {"mode": "persistent"},
  "sandbox": {"image": "python:3.12"}
}
```

All fields are optional. Defaults come from org config.

**Response (201):**
```json
{
  "session_id": "uuid",
  "status": "active",
  "created_at": "2025-01-01T00:00:00Z"
}
```

### `GET /v1/sessions`

List the current user's sessions (paginated).

**Query parameters:**
- `status` -- filter by status (`active`, `paused`, `completed`, `failed`)
- `limit` -- max results (default 20)
- `offset` -- pagination offset

**Response (200):**
```json
{
  "sessions": [
    {
      "id": "uuid",
      "status": "active",
      "title": "Debug auth middleware",
      "model": "claude-sonnet-4-20250514",
      "message_count": 12,
      "tool_call_count": 5,
      "estimated_cost_usd": 0.042,
      "created_at": "2025-01-01T00:00:00Z",
      "updated_at": "2025-01-01T00:05:00Z"
    }
  ],
  "total": 42
}
```

### `GET /v1/sessions/{id}`

Get session metadata and counters.

### `POST /v1/sessions/{id}/messages`

Send a message to a session. Triggers the agent loop.

**Request:**
```json
{
  "content": "Write a Python script that parses CSV files"
}
```

**Response (202):**
```json
{
  "event_id": 42,
  "status": "processing"
}
```

### `POST /v1/sessions/{id}/pause`

Pause an active session. The harness will stop after the current iteration.

### `POST /v1/sessions/{id}/resume`

Resume a paused session.

### `DELETE /v1/sessions/{id}`

Delete a session and its resources (events, workspace bucket).

## Events (SSE Streaming)

### `GET /v1/sessions/{id}/events`

Subscribe to a session's event stream via Server-Sent Events (SSE).

**Query parameters:**
- `after` -- event ID cursor. Only events after this ID are returned. Use `0` for all events.

**Response:** `Content-Type: text/event-stream`

```
data: {"id": 1, "type": "session.start", "data": {...}, "created_at": "..."}

data: {"id": 2, "type": "user.message", "data": {"content": "Hello"}, "created_at": "..."}

data: {"id": 3, "type": "llm.response", "data": {"content": "Hi! How can I help?"}, "created_at": "..."}

data: {"id": 4, "type": "tool.call", "data": {"name": "terminal", "arguments": {"command": "ls"}}, "created_at": "..."}

data: {"id": 5, "type": "tool.result", "data": {"name": "terminal", "result": "file1.py\nfile2.py"}, "created_at": "..."}
```

The SSE connection stays open. New events are pushed as they are emitted. If the connection drops, reconnect with `?after=<last-event-id>` to resume without data loss.

## Skills

### `GET /v1/skills`

List skills for the current tenant (merged: platform + org + user).

**Query parameters:**
- `type` -- filter by type (`skill` or `expert`)

**Response (200):**
```json
{
  "skills": [
    {
      "id": "uuid",
      "name": "code_reviewer",
      "description": "Reviews code for quality and security",
      "type": "skill",
      "enabled": true
    }
  ]
}
```

### `POST /v1/skills`

Create a new skill.

**Request:**
```json
{
  "name": "sql_writer",
  "description": "Writes PostgreSQL queries",
  "type": "expert",
  "content": "---\nname: sql_writer\n...\n---\nExpert prompt content...",
  "config": {}
}
```

### `GET /v1/skills/{id}`

Get skill details (includes full content and expert fields).

### `PUT /v1/skills/{id}`

Update a skill.

### `DELETE /v1/skills/{id}`

Delete a skill.

### Expert-Specific Actions

These endpoints are only valid for skills with `type=expert`:

| Endpoint | Description |
|---|---|
| `POST /v1/skills/{id}/collect` | Trigger training data export from event log |
| `GET /v1/skills/{id}/training-data` | List exported training datasets |
| `GET /v1/skills/{id}/training-data/{dataset_id}` | Download JSONL dataset |
| `POST /v1/skills/{id}/activate` | Set `expert_status` to `active` (requires endpoint) |
| `POST /v1/skills/{id}/retire` | Set `expert_status` to `retired` |

## Memory

### `GET /v1/memory`

Read memory files for the current tenant.

**Query parameters:**
- `target` -- `memory` (MEMORY.md) or `user` (USER.md)
- `scope` -- `shared` (org-wide) or `user` (user-specific)

**Response (200):**
```json
{
  "content": "...",
  "entries": ["entry1", "entry2"],
  "frozen_at": "2025-01-01T00:00:00Z"
}
```

### `POST /v1/memory`

Mutate memory.

**Request:**
```json
{
  "action": "add",
  "target": "memory",
  "text": "User prefers Python 3.12+ features"
}
```

Actions: `add`, `replace` (requires `old_text`), `remove` (requires `old_text`).

## Workspace Files

### `GET /v1/sessions/{id}/workspace/tree`

List files in the session's workspace.

### `GET /v1/sessions/{id}/workspace/files/{path}`

Read a file from the workspace.

### `PUT /v1/sessions/{id}/workspace/files/{path}`

Write a file to the workspace.

### `DELETE /v1/sessions/{id}/workspace/files/{path}`

Delete a file from the workspace.

## Tools and MCP

### `GET /v1/tools`

List available tools for the current tenant (builtin + MCP).

### `GET /v1/mcp/servers`

List configured MCP servers.

### `POST /v1/mcp/servers`

Add an MCP server configuration.

### `DELETE /v1/mcp/servers/{id}`

Remove an MCP server.

## Feedback (API Channel)

Service-account clients — typically an automated judge grading pipeline
output — record feedback against an `llm.response` or `expert.result`
event through the same handler that serves the web UI, mounted under
the `/v1/api/*` prefix so SA tokens can reach it.

### `POST /v1/api/sessions/{session_id}/events/{event_id}/feedback`

**Request:**
```json
{
  "rating": "up",
  "score": 0.87,
  "criteria": {"correctness": 0.9, "relevance": 0.85},
  "rationale": "Matches the reference; arithmetic is correct."
}
```

- `rating` (required, `"up"` or `"down"`) — binary bucket used by
  training-data selectors.
- `score` (optional, `0.0-1.0`) — numeric grade when the principal is a
  judge; ignored by bucket-oriented selectors.
- `criteria` (optional dict of string → float) — per-axis grades.
- `rationale` (optional, max 10,000 chars) — free-form text the judge
  produced.
- `reason` (optional, max 500 chars) — the shorter, human-UI-friendly
  explanation; interchangeable with `rationale` on the server side.

**Response (201):**
```json
{
  "event_id": 42,
  "event_type": "user.feedback",
  "source": "judge"
}
```

`source` is `"judge"` when the caller presented a service-account token
and `"user"` when the caller presented an interactive JWT.  Stored on
the event's JSONB payload so downstream training-data selection and
dashboards can weight the two independently.

**Idempotency.** Dedupe is keyed on `(session_id, event_id, principal)`
where `principal` is the caller's `user_id` for JWT callers and
`service_account_id` for SA callers.  A retry from the same principal
returns the original feedback event unchanged; feedback from a user
and from a judge on the same turn coexist as two independent events.

## Prompts (API Channel)

Fire-and-forget prompt submission for non-interactive clients. Requires a service-account token. Results are read back from the `events` table by `session_id`. See [Channels / API](../channels/api.md) for the end-to-end pipeline workflow.

### `POST /v1/api/prompts`

Submit a single prompt.

**Request:**
```json
{
  "prompt": "Write a haiku about distributed systems.",
  "idempotency_key": "dataset-42/row-1337",
  "metadata": {"dataset_id": "ds_123", "row_index": 1337}
}
```

- `prompt` (required, max 200,000 chars).
- `idempotency_key` (optional, max 200 chars) -- two submissions with the same key + org resolve to the same session; the second returns `deduplicated: true` and enqueues no new work.
- `metadata` (optional dict) -- stored on `sessions.config['pipeline_metadata']`; the pipeline joins results back to its dataset via this field.

**Response (202):**
```json
{
  "session_id": "8f...",
  "event_id": 42,
  "deduplicated": false,
  "error": null
}
```

### `POST /v1/api/prompts:batch`

Submit up to 100 prompts in one round-trip. Each entry is processed independently; partial failures surface per-slot, not as a whole-request 500 (unless every entry fails).

**Request:**
```json
{
  "prompts": [
    {"prompt": "...", "idempotency_key": "row-1"},
    {"prompt": "...", "idempotency_key": "row-2"}
  ]
}
```

**Response (202):**
```json
{
  "results": [
    {"session_id": "...", "event_id": 1, "deduplicated": false, "error": null},
    {"session_id": "...", "event_id": 2, "deduplicated": true, "error": null}
  ]
}
```

Input order is preserved so the caller can zip results back to its input rows.

## Admin

These endpoints require admin permissions.

### `GET /v1/admin/orgs`

List all organizations.

### `POST /v1/admin/orgs`

Create a new organization.

### `GET /v1/admin/orgs/{id}/users`

List users in an organization.

### `POST /v1/admin/orgs/{id}/users`

Create a user in an organization.

### `POST /v1/admin/orgs/{id}/channels/slack`

Install the Slack bot for an organization.

### Service Accounts (Admin) {#service-accounts-admin}

Issue and revoke service-account tokens that authenticate the API channel. All endpoints require the `admin` permission. Tokens have the prefix `surg_sk_`; the raw value is returned once on creation and is not recoverable.

#### `POST /v1/admin/service-accounts`

Issue a new token.

**Request:**
```json
{"org_id": "00000000-...", "name": "dataset-gen-v1"}
```

**Response (201):**
```json
{
  "id": "uuid",
  "org_id": "00000000-...",
  "name": "dataset-gen-v1",
  "token_prefix": "surg_sk_abcd1234",
  "created_at": "2025-01-01T00:00:00Z",
  "last_used_at": null,
  "revoked_at": null,
  "token": "surg_sk_<44 chars>"
}
```

Store the `token` immediately -- only the `token_prefix` is persisted afterwards.

#### `GET /v1/admin/service-accounts?org_id={id}`

List service accounts for an org. `token` is never returned.

#### `DELETE /v1/admin/service-accounts/{id}`

Revoke a service account. Immediate in the revoking process; peer API/worker processes converge within 60 seconds (the in-memory auth cache's TTL). A second delete on the same id returns 404.

## Health and Metrics

### `GET /health`

Health check endpoint (no auth required).

**Response (200):**
```json
{
  "status": "healthy",
  "components": {
    "database": "ok",
    "redis": "ok",
    "storage": "ok"
  }
}
```

### `GET /metrics`

Prometheus-compatible metrics (active sessions, event throughput, LLM latency, etc.).
