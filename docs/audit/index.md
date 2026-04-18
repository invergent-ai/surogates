# Audit &amp; Observability

Surogates keeps a complete, append-only record of every decision the
platform makes.  External audit, compliance, and training-data tooling
consumes the substrate directly at the database level — there is no
HTTP audit API, and views are the contract.

This section documents that substrate end-to-end:

- [**events.md**](events.md) — session-scoped event log.  Every
  conversation turn, tool call, governance decision, saga step, expert
  delegation, and user feedback inside a session.
- [**audit_log.md**](audit_log.md) — tenant-scoped audit log.  Events
  that happen *outside* any session: authentication, MCP tool safety
  scans, credential vault access.
- [**views.md**](views.md) — SQL views that project JSONB payloads into
  typed columns for dashboards, audit queries, and training-data
  selectors.

## Why two tables

The session event log and the audit log answer different questions and
have different foreign-key shapes.

| Question | Table | Scope |
|---|---|---|
| "What did agent X do for user Y in session Z?" | `events` | `session_id` required |
| "Who logged in last week?" | `audit_log` | `org_id` required, no session |
| "Did any MCP tool get rug-pulled?" | `audit_log` | Scans happen at server connect, not inside a session |
| "Which credentials did the GitHub MCP server read?" | `audit_log` | Credential resolution is tenant-level |

Trying to unify them on one table forced either a nullable
`session_id` (loses FK integrity for session events) or an
always-present fake session (hides the fact that auth has no session).
Two tables, one contract.

## The denormalization trigger

`events` carries `org_id` and `user_id` columns that are populated by
the `events_populate_tenant` trigger on insert — callers never set
them.  This lets audit queries filter by tenant directly:

```sql
SELECT * FROM events
WHERE org_id = $1 AND type = 'policy.denied'
  AND created_at > now() - interval '7 days';
```

Without denormalization the same query would join `sessions`, which is
the hot write path for session activity — expensive on busy clusters.

## Trace correlation

Every row in both tables carries optional `trace_id` and `span_id`
columns populated from the active request trace (via
`surogates.trace`).  External OpenTelemetry-style consumers can join
rows across tables by `trace_id` to reconstruct a full request —
e.g. login → MCP connect → session start → first tool call.

## Non-blocking emission

Audit writes must never break the user-facing flow they observe.
[`AuditStore.emit`](../../surogates/audit/store.py) swallows persistence
errors and logs them at `exception` level; the caller's business
logic continues regardless.

## Cross-table activity feed

A typical compliance dashboard unions both tables to build a complete
activity timeline:

```sql
SELECT 'session' AS source, session_id::text AS scope,
       type, data, created_at
FROM events
WHERE org_id = $1
UNION ALL
SELECT 'tenant', NULL,
       type, data, created_at
FROM audit_log
WHERE org_id = $1
ORDER BY created_at DESC
LIMIT 500;
```

## Stability policy

Both tables share one policy:

- **Documented JSONB keys are the contract.**  Renaming or removing a
  key is a breaking change; additions are always safe.
- **New event/audit types go into the enum and gain a doc entry in the
  same commit.**  Emit sites land alongside.
- **External consumers should read from views** when a view covers the
  query — views absorb JSONB key additions; raw queries do not.

## Recording your own events

Emit sites inside the platform:

| From | Call |
|---|---|
| Session-scoped (inside the harness, tool execution, etc.) | `await session_store.emit_event(session_id, EventType.X, payload)` |
| Tenant-scoped (auth, MCP scan, credentials) | `await audit_store.emit(org_id=…, type=AuditType.X, data=payload)` |

Payload builders live in
[`surogates/audit/events.py`](../../surogates/audit/events.py) and
[`surogates/governance/events.py`](../../surogates/governance/events.py)
— prefer those helpers over hand-building dicts so the stable JSONB
shape is preserved.
