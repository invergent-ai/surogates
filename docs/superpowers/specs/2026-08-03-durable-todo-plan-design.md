# Durable todo plan

## Problem

`TodoStore` instances live in a module-global dict keyed by session id
(`surogates/tools/builtin/todo.py`). Nothing persists them and nothing evicts
them. Three consequences:

1. A cold store returns `{"todos": [], "total": 0}` while the conversation
   history plainly shows a list — any new worker, pod, or wake starts blank.
2. `merge=true` against a cold store falls through to replace
   (`TodoStore.write` merges onto an empty `existing` map), silently dropping
   every item written in an earlier wake.
3. A long-running worker leaks one `TodoStore` per session it has ever
   processed.

The list itself is *not* lost — `tool_exec` emits `TOOL_CALL`/`TOOL_RESULT`
with the full payload and the replay path restores both — so the model still
sees its last list in history. This is a projection bug: the store disagrees
with the transcript.

## Design

### Event

`EventType.TODO_UPDATED = "todo.updated"`, payload `{"todos": [...]}`, emitted
by the todo handler **on write only**. No migration: `events.type` is plain
text with no CHECK constraint.

Every todo response already returns the complete list, so this is a snapshot
log, not a delta log. Recovery reads one row.

### Retrieval

`SessionStore.latest_todo_snapshot(session_id) -> list | None`

```sql
SELECT data FROM events
 WHERE session_id = :sid AND type = 'todo.updated'
 ORDER BY id DESC LIMIT 1
```

Served by `idx_events_session_type` over a handful of small rows. Contrast
scanning `tool.result`, whose payloads carry file contents, web pages and
terminal output.

`None` (never written) is distinct from `[]` (explicitly emptied). The merge
path needs that distinction.

### No store cache

The module-global dict is deleted rather than bounded. Once the snapshot is
the source of truth, the cache buys nothing and costs a leak: the handler
builds a `TodoStore` per call, applies the operation, and emits. A todo call
is rare (a handful per session) and the query is one indexed row.

This also removes `_get_store`, `format_for_injection` and `has_items`, none
of which have a caller outside the module.

### Tool classification

`todo` moves out of `CONCURRENCY_SAFE_TOOLS` — and therefore out of
`PARALLEL_TOOLS`, so it stops firing eagerly mid-stream — into a new
`DURABLE_STATE_TOOLS`, unioned into `BATCH_PARALLEL_TOOLS` so it still fans
out once the stream commits. It also leaves `SAGA_EXCLUDED_TOOLS`.

This is the rule `PARALLEL_TOOLS`' own docstring already states:

> Eager dispatch must be safe to cancel mid-flight, so side-effecting tools
> that allocate durable state (delegation tools create child sessions in the
> DB) are excluded even though they're safe to batch-dispatch concurrently
> after the stream completes.

A todo write now allocates durable state. Delegation tools are the existing
precedent; `DURABLE_STATE_TOOLS` names the category for tools that qualify
without being delegations.

## Out of scope

Post-compaction re-injection, the `projection_gap` marker, reviving
`format_for_injection`, any pydantic schema port, any model-facing API change.

## Tests

- Two wakes, no shared process state: wake 2's `merge=true` keeps wake 1's
  items.
- A cold read after a wake that wrote returns the list, not `[]`.
- `None` vs `[]`: a session that never wrote merges onto nothing; a session
  that emptied its list stays empty.
- The event fires on write, not on read.
- Routing: `todo` absent from `PARALLEL_TOOLS`, present in
  `BATCH_PARALLEL_TOOLS` — the regression that would silently restore
  mid-stream writes.
