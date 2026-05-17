# Subagent Task Layer — v1 Design

**Status**: Design approved, ready for implementation plan.
**Date**: 2026-05-16
**Scope**: Backend (`surogates/`) only. SDK + web UI explicitly deferred.

## 1. Goal

Add durable, DAG-aware, retry-with-history task semantics on top of Surogates's existing `spawn_worker` / `delegate_task` infrastructure, without replacing either. The result lets a coordinator agent express multi-step workflows that:

- Wait for multiple parents to complete before starting (fan-in synthesis)
- Survive a worker crash and retry with the same goal context
- Pause for additional input from the parent (`blocked` state) and resume with extra context
- Stop after N consecutive failures (circuit breaker)

A coordinator should reach for `spawn_task` when it needs any of those. `spawn_worker` remains the right primitive for fire-and-forget one-shots.

## 2. Non-goals (v1)

- **No board UI / dashboard.** Backend only. Add later if needed.
- **No comments/threads on tasks.** Cross-attempt context flows through `task.context` (appended on unblock); inter-task context flows through parent results.
- **No separate `task_runs` history table.** `sessions.task_id` gives "all attempts for task X" via `WHERE task_id = X ORDER BY created_at`.
- **No explicit `task_complete` tool.** Workers complete naturally; the existing `WORKER_COMPLETE` event is mapped to `task.status='done'`.
- **No hallucination guards** on cross-task references / phantom card ids (out of scope for v1).
- **No LLM-driven specifier / triage flow** for fleshing out terse goals.
- **No recursive task-spawning from children.** Children inherit the existing `WORKER_EXCLUDED_TOOLS` exclusion, extended to cover the new task tools.
- **No notification routing to external messengers** (Telegram/Slack/etc.).

## 3. Architectural context (what already exists)

Surogates ships a comprehensive subagent layer that this design extends rather than replaces:

| Existing | Location | Role |
|---|---|---|
| `delegate_task` tool | `tools/builtin/delegate.py` | Sync fork-join; parent blocks until child(ren) return. Has `agent_type`, depth limit, parallel `goals`, stale detection. |
| `spawn_worker` / `send_worker_message` / `stop_worker` | `tools/builtin/coordinator.py` | Async; worker is a full Session, enqueued via `enqueue_session`. Completion via `WORKER_COMPLETE` event re-enqueues parent. |
| `AgentDef` catalog | `tools/loader.py` + `harness/agent_resolver.py` | Per-tenant pre-configured roles: `system_prompt`, `tools` (allowlist), `disallowed_tools`, `model`, `max_iterations`, `policy_profile`. Resolved wake-time, non-destructively hydrated onto child session config. |
| Per-session tool filtering | `orchestrator/worker.py:686-702` (`_filter_effective_tools`) | The gate we extend (vs adding `check_fn` to the registry). |
| `agent_type` schema gating | `harness/tool_schemas.py` | Strips `agent_type` from tool schemas when tenant has no AgentDefs. We extend `_AGENT_TYPE_GATED_TOOLS`. |
| `create_child_session` | `session/provisioning.py` | The session-creation primitive both this layer and `spawn_worker` call. |
| `enqueue_session(redis, agent_id, session_id)` | `surogates/config.py` | Redis sorted-set work queue dispatch. |
| `INTERRUPT_CHANNEL_PREFIX` pub/sub | `surogates/config.py` | The signal `stop_worker` uses to interrupt a running session. We reuse for `task_block`. |

The gap this design closes: nothing today persists a goal separately from the executing Session, so DAG dependencies, retries, and human-in-the-loop blocking can't be expressed cleanly.

## 4. Data model

One new table, one join table, one column on `sessions`. New migration adds all three.

```
tasks
  id                  uuid     PK
  org_id              uuid     FK → orgs                 NOT NULL
  parent_session_id   uuid     FK → sessions             NOT NULL  -- session that spawned this
  agent_def_name      text                               NULL      -- key into existing AgentDef catalog
  goal                text                               NOT NULL
  context             text                               NULL      -- same shape as spawn_worker.context; appended on unblock
  current_session_id  uuid     FK → sessions             NULL      -- in-flight or last attempt
  status              text                               NOT NULL  -- todo|ready|running|blocked|done|failed|cancelled
  result              text                               NULL
  blocked_reason      text                               NULL
  attempt_count       int      NOT NULL DEFAULT 0
  max_attempts        int      NOT NULL DEFAULT 3
  created_at, started_at, completed_at  timestamptz

task_links
  parent_id  uuid  FK → tasks(id)
  child_id   uuid  FK → tasks(id)
  PRIMARY KEY (parent_id, child_id)

sessions
  + task_id  uuid  FK → tasks(id)  NULL  -- when set, this session is one attempt of that task
```

### 4.1 Status semantics

| Status | Owner | Meaning |
|---|---|---|
| `todo` | tick | Created; one or more parents not yet `done` |
| `ready` | tick | All parents done (or none); eligible for spawn |
| `running` | tick | A Session is executing this task; `current_session_id` set |
| `blocked` | `task_block` tool | Worker paused itself awaiting more context |
| `done` | tick | Worker session completed normally; `result` written |
| `failed` | tick | `attempt_count >= max_attempts` after crashes/timeouts |
| `cancelled` | `cancel_task` tool | Explicitly aborted by spawning parent |

Cancelled/failed parents **do not** unblock children — the orchestrator must explicitly cancel/replan downstream. Same semantics as Hermes Kanban.

### 4.2 Multi-tenancy

Every Task scoped by `org_id`; `task_links` and `sessions.task_id` inherit scope via FK. No new tenancy concept.

## 5. Tool surface

Four new tools. Existing tools (`delegate_task`, `spawn_worker`, `send_worker_message`, `stop_worker`) unchanged.

### 5.1 Coordinator tools (same gating as `spawn_worker`)

```
spawn_task(
    goal: str,                          # required
    context: str | None = None,         # same shape as spawn_worker.context
    agent_type: str | None = None,      # AgentDef name from existing catalog
    parents: list[str] = [],            # task ids this depends on (fan-in DAG)
    max_attempts: int | None = None,    # default 3
) -> {"task_id": str, "status": "todo" | "ready" | "running"}
```

- Creates a Task row. Status is `ready` if `parents=[]` or all parents already `done`, else `todo`.
- If `ready`: the handler **eagerly** creates the child Session via `_create_session_for_task` (factored from `_spawn_worker_handler`) to avoid up-to-5s tick latency; returns `status='running'`.
- If `todo`: returns immediately; the tick promotes it later when parents complete.
- Validates: parents exist, parents are in same org, no cycles, no self-link.

```
unblock_task(
    task_id: str,
    additional_context: str | None = None,
) -> {"ok": bool}
```

- Ownership check: `task.parent_session_id == current session.id`.
- Status must be `blocked`. Sets to `ready`. Clears `blocked_reason`.
- `additional_context` is appended to `task.context` with a timestamp marker so the next Session attempt sees it.

```
cancel_task(
    task_id: str,
    reason: str | None = None,
) -> {"ok": bool}
```

- Ownership check, same as `unblock_task`.
- Status must be non-terminal (`todo` / `ready` / `running` / `blocked`).
- If `running`: publishes to `INTERRUPT_CHANNEL_PREFIX<session_id>` (the same channel `stop_worker` uses) to interrupt the current Session.
- Sets `status='cancelled'`, `completed_at=now`.

### 5.2 Subagent self-tool (gated on `Session.task_id IS NOT NULL`)

```
task_block(reason: str) -> {"ok": bool}
```

- Self-block only. Handler reads `task_id` from the calling Session — no `task_id` arg.
- Sets `task.status='blocked'`, `task.blocked_reason=reason`. Does **not** increment `attempt_count`.
- Emits `TASK_BLOCKED` event to `parent_session_id`.
- Publishes to `INTERRUPT_CHANNEL_PREFIX<session_id>` so the harness loop exits cleanly.

### 5.3 Gating mechanism

No `check_fn` extension to the tool registry is required. The existing per-session filtering in `_filter_effective_tools` at `orchestrator/worker.py:686-702` is the gate:

```python
# Added at the bottom of _filter_effective_tools, after existing kb/anonymous filtering:
if session.task_id is None:
    effective_tools.discard("task_block")
```

Coordinator-side tools (`spawn_task` / `unblock_task` / `cancel_task`) are gated identically to `spawn_worker` — by inclusion in the parent's `AgentDef.tools` allowlist, and by exclusion from child sessions via:

```python
# In tools/builtin/coordinator.py, extend the existing constant:
WORKER_EXCLUDED_TOOLS: frozenset[str] = frozenset({
    "spawn_worker", "send_worker_message", "stop_worker",
    "spawn_task", "unblock_task", "cancel_task",     # new
})
```

The `agent_type` schema-stripper at `harness/tool_schemas.py` needs `"spawn_task"` added to `_AGENT_TYPE_GATED_TOOLS` so tenants with no AgentDefs don't see the parameter.

## 6. Dispatcher + completion flow

### 6.1 New module layout

```
surogates/tasks/
  models.py              # Task, TaskLink (SQLAlchemy); sessions.task_id added in same migration
  tools.py               # spawn_task, unblock_task, cancel_task, task_block handlers + schemas
  dispatcher.py          # tasks_tick(): promote + finalize + retry/enqueue
  completion.py          # session-end → task state mapping (called from tick)
  spawn.py               # _create_session_for_task() — factored from _spawn_worker_handler

surogates/db/migrations/<timestamp>_add_subagent_task_layer.py
```

`spawn.py`'s `_create_session_for_task` is the shared spawn primitive: factored from lines 246-325 of `tools/builtin/coordinator.py` so both the `spawn_task` tool handler and the tick can call it. Tiny extraction, no behavior change for existing `spawn_worker`.

### 6.2 `tasks_tick()` — 5s cadence

Three SQL-driven steps:

**Step 1 — Promote `todo → ready`:**

```sql
UPDATE tasks SET status = 'ready'
 WHERE status = 'todo'
   AND NOT EXISTS (
     SELECT 1 FROM task_links tl
     JOIN tasks p ON p.id = tl.parent_id
     WHERE tl.child_id = tasks.id
       AND p.status != 'done'
   );
```

**Step 2 — Finalize tasks whose Session ended:**

```sql
SELECT t.id, t.attempt_count, t.max_attempts, s.id AS sid, s.ended_at
  FROM tasks t
  JOIN sessions s ON s.id = t.current_session_id
 WHERE t.status = 'running'
   AND s.ended_at IS NOT NULL;
```

For each row, inspect the Session's final event:

- `WORKER_COMPLETE` was emitted → set `task.status='done'`, `task.result = event.payload.result`, `task.completed_at = now`. Recompute children (re-run Step 1 once).
- `TASK_BLOCKED` was emitted (from `task_block` tool) → already handled by the tool. Belt-and-suspenders: if status was still `running`, set to `blocked` from event payload.
- No completion event (crash, lease expiry, timeout) → bump `attempt_count`. If `attempt_count < max_attempts`: `status='ready'` (retry). Else: `status='failed'`, emit `TASK_FAILED` to parent.

**Step 3 — Enqueue `ready` tasks** (atomic claim via Postgres):

```python
async with db.begin():
    task = await db.scalar(
        select(Task).where(Task.status == 'ready')
                    .with_for_update(skip_locked=True)
                    .limit(1)
    )
    if not task: return
    session = await _create_session_for_task(task, ...)
    task.current_session_id = session.id
    task.status = 'running'
    task.attempt_count += 1
    task.started_at = task.started_at or func.now()
await enqueue_session(redis, session.agent_id, session.id)
```

Loop with per-tick spawn cap of 10 to prevent a flood of newly-ready tasks from monopolizing one tick.

### 6.3 `_create_session_for_task` — the spawn primitive

```python
async def _create_session_for_task(task, *, session_store, session_factory, tenant):
    parent = await session_store.get_session(task.parent_session_id)
    agent_def = await resolve_agent_by_name(
        task.agent_def_name, tenant, session_factory=session_factory,
    ) if task.agent_def_name else None

    worker_config = _build_worker_config(agent_def, task)  # mirrors existing spawn_worker logic
    if task.agent_def_name:
        worker_config["agent_type"] = task.agent_def_name

    child = await create_child_session(
        store=session_store, parent=parent, channel="task",
        model=(agent_def.model if agent_def else None),
        config=worker_config,
    )
    child.task_id = task.id

    user_msg = task.goal
    if task.context:
        user_msg = f"{task.goal}\n\n## Context\n{task.context}"
    await session_store.emit_event(child.id, EventType.USER_MESSAGE, {"content": user_msg})
    await session_store.emit_event(
        task.parent_session_id, EventType.WORKER_SPAWNED,
        {"worker_id": str(child.id), "task_id": str(task.id), "goal": task.goal},
    )
    return child
```

Uses `channel="task"` (vs `"worker"`) so the channel field cleanly distinguishes task-backed Sessions from plain `spawn_worker` Sessions — supports future UI filtering without schema changes.

### 6.4 Event vocabulary — three new types

Extend `EventType` enum in `surogates/session/events.py`:

| Event | Emitted to | When | Payload (minimum) |
|---|---|---|---|
| `TASK_BLOCKED` | parent session | Worker calls `task_block` | `{task_id, reason, worker_id}` |
| `TASK_FAILED` | parent session | Tick sees `attempt_count >= max_attempts` after a crash/timeout | `{task_id, worker_id, attempt_count}` |
| (existing `WORKER_COMPLETE`) | parent session | Worker session ends naturally | Existing payload **+** `task_id` when `session.task_id` is set |

Parent's harness already wakes on event arrivals (`orchestrator/dispatcher.py:240`). No new wake mechanism. Parent's next turn sees the event as a tool-result equivalent.

### 6.5 Tick host

Primary: register `tasks_tick` in `surogates/jobs/` alongside `cleanup_sessions.py` / `inbox_expire.py`, at a 5s cadence.

Fallback: if the jobs scheduler's minimum cadence is coarser than 5s (some periodic jobs run at minute granularity), host the tick inside `surogates/orchestrator/dispatcher.py` next to the existing worker loop. The implementation plan should verify which path applies and commit to one.

## 7. Open integration points (to resolve at implementation time)

1. **Jobs scheduler cadence.** Verify whether the existing scheduler in `surogates/jobs/` supports 5s ticks; if not, host the tick in the orchestrator dispatcher loop. No design impact, only file location.
2. **`_filter_effective_tools` signature.** Confirm `session.task_id` is accessible at the point this function is called; if not, plumb it through (small change). Lines 686-702 of `orchestrator/worker.py`.
3. **`WORKER_COMPLETE` payload extension.** Confirm `worker_notify.py`'s emission point sees the worker session's `task_id`; if not, plumb the task_id through.
4. **Harness reaction to `INTERRUPT_CHANNEL_PREFIX` for `task_block`.** Verify the existing interrupt-handling path in the harness loop terminates the session cleanly without emitting a spurious crash event. Use the existing `stop_worker` semantics.

None of these change the design — they're integration verifications.

## 8. Implementation plan stub (file-level)

Concrete artifacts (an implementation plan will sequence these into PRs):

**New:**
- `surogates/tasks/__init__.py`
- `surogates/tasks/models.py` — `Task`, `TaskLink`
- `surogates/tasks/tools.py` — 4 tool handlers + schemas; calls `tools.registry.ToolRegistry.register`
- `surogates/tasks/dispatcher.py` — `tasks_tick`
- `surogates/tasks/completion.py` — session-end → task mapping
- `surogates/tasks/spawn.py` — `_create_session_for_task` factored from coordinator.py
- `surogates/db/migrations/<ts>_add_subagent_task_layer.py` — tasks + task_links + sessions.task_id
- `surogates/jobs/tasks_tick.py` — scheduler registration (or orchestrator integration, see §7.1)
- `tests/tasks/test_*.py` — unit + integration coverage (claim atomicity, DAG promotion, retry, block/unblock, cancel, multi-tenancy isolation)

**Modified:**
- `surogates/session/models.py` — add `task_id` column
- `surogates/session/events.py` — add `TASK_BLOCKED`, `TASK_FAILED` to `EventType`
- `surogates/harness/worker_notify.py` — include `task_id` in `WORKER_COMPLETE` payload when session has one
- `surogates/orchestrator/worker.py` — `_filter_effective_tools` adds `task_block` gating
- `surogates/harness/tool_schemas.py` — add `"spawn_task"` to `_AGENT_TYPE_GATED_TOOLS`
- `surogates/tools/builtin/coordinator.py` — extend `WORKER_EXCLUDED_TOOLS`; factor out spawn helpers used by `spawn.py`
- `surogates/tools/builtin/__init__.py` (or wherever tools are registered) — register `surogates.tasks.tools`

**Unchanged:**
- `delegate_task` (sync, in-process subagents stay the same)
- `spawn_worker` / `send_worker_message` / `stop_worker` (low-level primitives stay the same)
- `AgentDef` catalog, `agent_resolver`, `apply_agent_def_to_session`
- The Redis work queue and session lease mechanics
