# Shared Child-Session Workspace — Design

## Goal

Child sessions created by `delegate_task`, `spawn_worker` (coordinator), and the scheduled-session runner (one-shot and dynamic loops) must operate on the same workspace as their parent. Today only delegation children share via the K8s sandbox pool; coordinator children share via an ad-hoc `workspace_path` copy; loop iterations get fresh empty workspaces. Unify all three under one mechanism.

## Background

Three identifiers govern workspace sharing today:

- `storage_bucket` — configured workspace bucket name (or local backend bucket).
- `workspace_path` — the resolved filesystem path tools use for governance checks and process-sandbox execution. For S3 this is always `"/workspace"`; for local it's `{base}/{bucket}/sessions/{session_id}/`.
- `sandbox_root_session_id` — cached id of the ultimate ancestor; used by `sandbox_session_key()` so delegation grandchildren still land on the original root's sandbox pod.

Current state per spawn path:

| Path | `storage_bucket` | `workspace_path` | `sandbox_root_session_id` |
|---|---|---|---|
| `delegate_task` ([delegate.py:175-198](../../../surogates/tools/builtin/delegate.py)) | Inherited from parent | **Not set** (gap) | Set to root |
| `spawn_worker` ([coordinator.py:295-298](../../../surogates/tools/builtin/coordinator.py)) | Not set | Copied from parent | Not set |
| Scheduled runner ([scheduled/runner.py:108-124](../../../surogates/scheduled/runner.py)) | Allocated fresh via `create_agent_session()` | Allocated fresh | Not set |

Consequences:

- Delegation children pass governance checks only because [tool_exec.py:853](../../../surogates/harness/tool_exec.py) skips the gate when `workspace_path` is absent — silent enforcement loss.
- Coordinator workers work today but bypass the canonical `sandbox_root_session_id` mechanism.
- Every loop iteration gets an empty workspace; multi-tick state is impossible without an external store.

A separate latent bug: [tool_exec.py:1027](../../../surogates/harness/tool_exec.py) builds the K8s mount source as `s3://{bucket}/{session_workspace_prefix(session.id)}` but provisions the pod with `sandbox_owner = sandbox_session_key(session)`. The owner resolves to the root; the mount source uses the child's id. The two disagree on reprovision.

## Non-goals

- Migrating existing rows. Pre-deploy children keep whatever workspace they have. `sandbox_session_key()` already falls back to `parent_id` when `sandbox_root_session_id` is absent, so legacy delegations remain correct.
- Reference-counted cleanup. The cleanup job continues to delete `sessions/{root}/` prefixes when the root session row is gone. A child outliving its root may lose files — accepted tradeoff.
- Changes to `expert_loop` (no child session is created).
- Changes to user-initiated session creation (web/API channels remain root sessions).

## Design

### Resolution rule

One rule, used everywhere a child needs to find its workspace:

```
root_id = session.config.get("sandbox_root_session_id")
       or session.parent_id
       or session.id
```

This is already what [`sandbox_session_key()`](../../../surogates/sandbox/pool.py) computes. The change is to *populate* `sandbox_root_session_id` from every child-creation path so the cache hit is consistent.

### New helper: `create_child_session()`

Add to [surogates/session/provisioning.py](../../../surogates/session/provisioning.py) alongside `create_agent_session()`:

```python
async def create_child_session(
    *,
    store: SessionStore,
    parent: Session,
    channel: str,
    model: str | None = None,
    config: dict | None = None,
    service_account_id: UUID | None = None,
    idempotency_key: str | None = None,
    session_id: UUID | None = None,
) -> Session:
    """Create a session that shares its parent's workspace.

    Copies storage_bucket, workspace_path, and supports_vision from the
    parent. Stamps sandbox_root_session_id = parent's root (or parent.id
    if parent is itself a root). Does NOT allocate a new workspace
    prefix on storage; the child writes into the root's sessions/{root}/
    prefix.

    Inherits agent_id, org_id, user_id, and service_account_id from
    parent. model defaults to parent.model when not overridden.
    """
```

Implementation:

1. Build `merged_config = dict(config or {})`.
2. Overwrite `storage_bucket`, `workspace_path`, and `supports_vision` from the parent's config. These are structural sharing fields; caller-provided child config must not change them.
3. Stamp `sandbox_root_session_id = sandbox_session_key(parent)` (equivalent to `parent.config.get("sandbox_root_session_id") or parent.parent_id or parent.id`). This preserves roots for new children of legacy child sessions too.
4. Resolve `effective_service_account_id = service_account_id if service_account_id is not None else parent.service_account_id`.
5. If `effective_service_account_id` is not None, set `merged_config["service_account_id"] = str(effective_service_account_id)` to preserve the config metadata that API sessions already carry.
6. Call `store.create_session(parent_id=parent.id, agent_id=parent.agent_id, org_id=parent.org_id, user_id=parent.user_id, channel=channel, model=model or parent.model, config=merged_config, service_account_id=effective_service_account_id, idempotency_key=idempotency_key, session_id=session_id)`.

The helper takes no `storage` argument and performs no S3 calls. The root's bucket already exists; the child reuses the same prefix.

### Callsite changes

**[delegate.py](../../../surogates/tools/builtin/delegate.py)** — replace the manual `child_config` storage/root stamping and direct `session_store.create_session(...)` call with `create_child_session(store=session_store, parent=parent_session, channel="delegation", model=model_override, config=child_config)`. The remaining fields in `child_config` (`max_iterations`, `streaming`, `agent_type`, `allowed_tools`, `excluded_tools`, `policy_profile`) stay as-is.

**[coordinator.py](../../../surogates/tools/builtin/coordinator.py)** — remove the explicit `worker_config["workspace_path"] = parent_session.config.get("workspace_path")` block. Replace the direct `session_store.create_session(...)` with `create_child_session(store=session_store, parent=parent_session, channel="worker", model=model_override, config=worker_config)`. The `WORKER_SPAWNED` event emission and redis enqueue afterwards stay unchanged.

**[scheduled/runner.py](../../../surogates/scheduled/runner.py)** — import `SessionNotFoundError` and branch on `schedule.created_from_session_id`:

```python
parent = None
if schedule.created_from_session_id is not None:
    try:
        parent = await self._session_store.get_session(schedule.created_from_session_id)
    except SessionNotFoundError:
        parent = None

if parent is not None:
    session = await create_child_session(
        store=self._session_store,
        parent=parent,
        channel="scheduled",
        model=self._settings.llm.model,
        config={
            "scheduled_session_id": str(schedule.id),
            "scheduled_source": schedule.source,
            "scheduled_dynamic_loop": is_dynamic_loop,
        },
        idempotency_key=idempotency_key,
    )
else:
    session = await create_agent_session(... existing call ...)
```

The detached-schedule fallback preserves today's behavior (fresh workspace) so a deleted creator doesn't crash the runner. The `IntegrityError` retry on `idempotency_key` wraps both branches.

### K8s mount-prefix fix

Change [tool_exec.py:1017-1040](../../../surogates/harness/tool_exec.py) to derive the mount source from the same identifier the pool keys on:

```python
sandbox_owner = sandbox_session_key(session)
sandbox_spec.resources.append(
    Resource(
        source_ref=f"s3://{storage_bucket}/{session_workspace_prefix(sandbox_owner)}",
        mount_path="/workspace",
    ),
)
await sandbox_pool.ensure(sandbox_owner, sandbox_spec)
```

This makes the spec idempotent — re-provisioning under a child mounts the root's prefix, not an empty child prefix.

## Data flow

```
delegate_task            ─┐
spawn_worker             ─┼─► create_child_session(parent, ...)
scheduled (with creator) ─┘        │
                                   ▼
                         store.create_session(
                             parent_id=parent.id,
                             config={
                               storage_bucket=parent.config["storage_bucket"],
                               workspace_path=parent.config["workspace_path"],
                               sandbox_root_session_id=sandbox_session_key(parent),
                               ...
                             })
                                   │
                                   ▼ (later, in harness loop)
                         tool_exec → sandbox_session_key(session) → root_id
                                   │
                                   ▼
                         sandbox_pool.ensure(root_id, spec)
                                   │
                                   ▼
                         pod mounts s3://bucket/sessions/{root_id}/
```

User-initiated sessions (web channel, API channel, scheduled with deleted creator) still flow through `create_agent_session()` and allocate fresh prefixes.

## Edge cases

| Case | Behavior |
|---|---|
| Grandchild (delegate of a delegate) | Helper reads `parent.config["sandbox_root_session_id"]`; grandchild stamps the same value. Pool resolves all three to one pod. |
| Loop iteration N+1 | `parent_id = schedule.created_from_session_id` for every iteration; all iterations resolve to the creator's root. |
| Detached schedule (creator session deleted) | `get_session()` raises `SessionNotFoundError`; runner catches it, falls back to `create_agent_session()`, and allocates a fresh workspace. Loop continues to run but loses cross-iteration continuity. |
| Child outlives root (cleanup deletes root prefix) | Child loses files. Accepted tradeoff. |
| Legacy children (no `sandbox_root_session_id`) | `sandbox_session_key()` already falls back to `parent_id`. No regression. |
| Local backend `workspace_path` | Helper copies the parent's path verbatim. It points under `sessions/{root}/` which the root already created. |
| Cross-tenant delegation | Not possible — helper inherits `org_id`, `user_id`, `agent_id` from parent. |

## Testing

1. `tests/test_session_provisioning.py::test_create_child_session_inherits_workspace` — child config has parent's `storage_bucket`, `workspace_path`; `sandbox_root_session_id` equals parent's id (parent is root).
2. `tests/test_session_provisioning.py::test_create_child_session_grandchild_preserves_root` — when parent already has `sandbox_root_session_id`, grandchild inherits the same value (not parent's id).
3. `tests/test_session_provisioning.py::test_create_child_session_does_not_allocate_prefix` — with `LocalBackend`, assert no new `sessions/{child_id}/` directory exists after creation.
4. `tests/test_session_provisioning.py::test_create_child_session_inherits_service_account` — service-account parent → child inherits `service_account_id` when none is passed.
5. `tests/test_agent_type_spawn.py` (or a new delegate-focused test file) — delegate's child config now has non-None `workspace_path` (regression for current gap that disables the governance gate on delegation children).
6. `tests/integration/test_scheduled_runner.py::test_loop_iteration_shares_creator_workspace` — claim a dynamic-loop schedule, run two ticks, assert both sessions resolve to the same root via `sandbox_session_key()`.
7. `tests/integration/test_scheduled_runner.py::test_detached_schedule_falls_back_to_fresh_workspace` — schedule whose creator session has been deleted; runner uses `create_agent_session()` and allocates a fresh prefix.
8. `tests/test_tool_exec.py::test_sandbox_mount_uses_root_prefix` — child session with `sandbox_root_session_id` set; mount `source_ref` ends with `sessions/{root_id}/`, not `sessions/{child_id}/`.

## Out of scope

- DB migration backfilling `sandbox_root_session_id` on existing rows.
- Changes to `surogates/jobs/cleanup_sessions.py`.
- Changes to `expert_loop` (same-session tool).
- Changes to user-facing session-creation endpoints.
- Reference-counted workspace lifetime.
