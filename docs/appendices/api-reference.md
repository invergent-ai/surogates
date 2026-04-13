# Appendix B: REST API Reference

The REST API serves two roles: the web channel interface (browser SPA talks directly to these endpoints) and the internal API consumed by workers and channel adapters.

Base URL: `/v1`

All endpoints require JWT authentication unless noted otherwise. The JWT is sent as `Authorization: Bearer <token>`.

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
