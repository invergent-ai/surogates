# Tenant-Scoped Audit Log (`audit_log`)

The [`events`](events.md) table captures everything that happens
inside a session.  `audit_log` captures everything that happens
*outside* any session: authentication, MCP tool safety scans,
credential vault access.  Both feed the same external audit consumers
— pick the right table based on whether the action is bound to a
session.

## Table shape

```sql
CREATE TABLE audit_log (
    id          BIGSERIAL PRIMARY KEY,
    org_id      UUID NOT NULL REFERENCES orgs(id),
    user_id     UUID REFERENCES users(id),    -- nullable: some entries are org-scoped
    type        TEXT NOT NULL,                -- see "Audit types" below
    data        JSONB,
    trace_id    TEXT,
    span_id     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### Indexes

| Index | Use case |
|---|---|
| `(org_id, type, created_at)` | "All failed logins for org X last 7d." |
| `(type, created_at)` | "All rug-pull detections platform-wide." |
| `(user_id, created_at)` | Per-user activity across all audit types. |

## Audit types

Authoritative list:
[`surogates/audit/types.py`](../../surogates/audit/types.py)
(`AuditType` enum).  The sections below document the stable JSONB
shape of each.

### Authentication

`auth.login` — a user successfully authenticated.  Emitted from the
`/v1/auth/login` endpoint.

| key | type | notes |
|---|---|---|
| `method` | string | `password`, `oidc`, `ldap` — how the user proved identity. |
| `provider` | string | Which provider validated credentials (default `database`). |
| `source_ip` | string | Request IP. Read from `X-Forwarded-For` (leftmost), then `X-Real-IP`, then the direct peer — see [`audit/request_meta.py`](../../surogates/audit/request_meta.py). In production uvicorn must run with `--proxy-headers` or the K8s ingress IP leaks here. |
| `timestamp` | number | Unix epoch seconds. |

`auth.failed` — a login attempt failed.  `user_id` is `NULL` because
by definition the user could not be identified.

| key | type | notes |
|---|---|---|
| `method` | string | |
| `provider` | string | |
| `reason` | string | Provider-supplied reason (e.g. `"user not found"`). |
| `source_ip` | string | |
| `attempted_email` | string | Only when the caller explicitly records it. |
| `timestamp` | number | |

### MCP tool safety

`policy.mcp_scan` — emitted once per advertised MCP tool during server
connect, carrying the verdict from `MCPGovernance.scan_tool` (AGT
scanner + local pattern checks).

| key | type | notes |
|---|---|---|
| `server` | string | Original (un-prefixed) MCP server name. |
| `tool` | string | MCP tool name, as advertised by the server. |
| `safe` | bool | `false` means the tool was filtered out before advertising. |
| `threats` | string[] | Human-readable threat descriptions; empty when safe. |
| `severity` | string | `info`, `warning`, or `critical`. |
| `timestamp` | number | |

`policy.rug_pull` — emitted when a previously-registered MCP tool's
SHA-256 fingerprint changes between connects (the server altered its
own tool definition).  Fingerprints are tracked per-tenant — a
rug-pull in one org's server never suppresses scans in another's.

| key | type | notes |
|---|---|---|
| `server` | string | |
| `tool` | string | |
| `previous_fingerprint` | string | SHA-256 hex of the prior definition. |
| `current_fingerprint` | string | SHA-256 hex of the current definition. |
| `timestamp` | number | |

### Credential vault

`credential.access` — emitted by the MCP proxy every time the
credential vault is queried to satisfy an MCP server's
`credential_refs`.  One entry per credential ref per connect.

| key | type | notes |
|---|---|---|
| `credential` | string | Name of the credential looked up. |
| `consumer` | string | What needed it, e.g. `mcp_server:github`. |
| `scope` | string | `user` / `org` / `missing` — which vault answered. |
| `found` | bool | `false` when the credential was not found. |
| `timestamp` | number | |

## Who writes what

| Emitter | Types |
|---|---|
| `POST /v1/auth/login` | `auth.login`, `auth.failed` |
| `surogates.mcp_proxy.pool.ConnectionPool.ensure_connected` | `policy.mcp_scan`, `policy.rug_pull` |
| `surogates.mcp_proxy.loader._resolve_credentials` | `credential.access` |

All emitters funnel through
[`AuditStore.emit`](../../surogates/audit/store.py) which swallows
persistence errors — audit logging must never break the user-facing
flow it observes.

## Querying

See [index.md](index.md#cross-table-activity-feed) for the cross-table
activity feed that unions `audit_log` with `events`.  Typical
audit-only queries:

```sql
-- Failed logins for an org this week
SELECT data->>'source_ip' AS ip,
       data->>'reason'    AS reason,
       created_at
FROM audit_log
WHERE org_id = $1
  AND type = 'auth.failed'
  AND created_at > now() - interval '7 days'
ORDER BY created_at DESC;

-- Every rug-pull platform-wide
SELECT org_id,
       data->>'server' AS server,
       data->>'tool'   AS tool,
       created_at
FROM audit_log
WHERE type = 'policy.rug_pull'
ORDER BY created_at DESC;
```
