"""Subagent task layer tools.

Public surface (all registered via :func:`register`):

* ``spawn_task``    — create a durable subagent task with optional DAG
                      parents and ``max_attempts``; eagerly spawns when
                      no parents are pending.
* ``unblock_task``  — orchestrator-only; resume a blocked task with
                      optional additional context.
* ``cancel_task``   — orchestrator-only; abort a non-terminal task.
* ``task_block``    — self-tool gated on ``Session.task_id``; pauses the
                      current attempt without consuming a retry.

All four are registered into the ``"core"`` toolset. Per-session gating
(``task_block`` only visible when ``Session.task_id is not None``) lives
in ``surogates.orchestrator.worker._filter_effective_tools`` to match
the pattern used for other context-conditional tools. Children spawned
via either ``spawn_worker`` or ``spawn_task`` inherit the
``WORKER_EXCLUDED_TOOLS`` exclusion so they cannot recursively spawn
tasks.

The handlers all receive their dependencies via keyword arguments
injected by the harness's tool-dispatch loop:

* ``session_store``  — :class:`SessionStore` for child-session creation
                       and event emission.
* ``redis``          — async :class:`Redis` client for the work queue
                       and the interrupt pub/sub channel.
* ``tenant``         — tenant context (carries ``org_id`` for FK scope).
* ``session_id``     — the calling Session's id as ``str``.
* ``session_factory``— async sessionmaker for direct task-row mutations.

Tool results are JSON strings; on success ``{"ok": true, ...}`` or
``{"task_id": ..., "status": ...}``, on failure ``{"error": "..."}``.
The harness loop coerces them to tool-result content for the LLM.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update

from surogates.config import INTERRUPT_CHANNEL_PREFIX, enqueue_session
from surogates.db.models import Session as ORMSession, Task, TaskLink
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool result helpers
# ---------------------------------------------------------------------------


def _tool_error(msg: str) -> str:
    return json.dumps({"error": msg})


def _tool_ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields})


def _parse_uuid_list(raw: Any, *, field_name: str) -> tuple[list[UUID] | None, str | None]:
    """Parse a list of task-id strings into UUIDs, deduplicated, preserving order.

    Returns ``(ids, None)`` on success or ``(None, error_message)`` on
    failure.  Non-list input or malformed UUIDs both fail.
    """
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return None, f"{field_name} must be a list of task ids"
    seen: set[UUID] = set()
    ids: list[UUID] = []
    for item in raw:
        try:
            u = UUID(str(item))
        except (ValueError, TypeError):
            return None, f"{field_name}: {item!r} is not a valid task id"
        if u in seen:
            continue
        seen.add(u)
        ids.append(u)
    return ids, None


# ---------------------------------------------------------------------------
# Tool schemas — descriptions are LLM-facing prose and double as docs.
# ---------------------------------------------------------------------------


_SPAWN_TASK_SCHEMA = ToolSchema(
    name="spawn_task",
    description=(
        "Spawn a durable subagent task. The task survives the parent's "
        "crash, can wait on multiple parent tasks (fan-in), retries on "
        "transient failure, and supports human-or-parent block/unblock "
        "via task_block + unblock_task. Use this when work must outlive "
        "a single LLM turn, depend on prior tasks, or be inspectable "
        "before completion. Prefer spawn_worker when the work is "
        "fire-and-forget and a single attempt is acceptable."
    ),
    parameters={
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "Complete, self-contained description of what the "
                    "subagent should accomplish. Subagents do not see "
                    "your conversation; include all necessary context."
                ),
            },
            "context": {
                "type": "string",
                "description": (
                    "Optional additional context appended to the goal "
                    "as a markdown '## Context' section in the worker's "
                    "first user message."
                ),
            },
            "agent_type": {
                "type": "string",
                "description": (
                    "Optional name of a pre-configured sub-agent type "
                    "from the tenant catalog. Inherits system prompt, "
                    "tool filter, model, and iteration cap. Consult "
                    "'# Available Sub-Agents' in your system prompt."
                ),
            },
            "parents": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Task ids this task depends on. Stays in 'todo' "
                    "until every parent reaches 'done'; promoted to "
                    "'ready' (and then immediately claimed) when all "
                    "parents complete. Cancelled or failed parents do "
                    "NOT promote children — orchestrate explicitly."
                ),
            },
            "max_attempts": {
                "type": "integer",
                "description": (
                    "Retry budget. Default 3. The dispatcher gives up "
                    "after this many consecutive crash/timeout attempts "
                    "and transitions the task to 'failed'."
                ),
            },
        },
        "required": ["goal"],
        "additionalProperties": False,
    },
)


_UNBLOCK_TASK_SCHEMA = ToolSchema(
    name="unblock_task",
    description=(
        "Resume a blocked subagent task. Only the spawning parent session "
        "can unblock its own children. Optional additional_context is "
        "appended to the task and delivered as part of the next attempt's "
        "initial input, so the next worker sees the new information."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "additional_context": {
                "type": "string",
                "description": "Extra context to give the next attempt.",
            },
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
)


_CANCEL_TASK_SCHEMA = ToolSchema(
    name="cancel_task",
    description=(
        "Cancel a non-terminal subagent task. Only the spawning parent "
        "session can cancel its children. If the task is currently "
        "running, its in-flight Session attempt is interrupted via the "
        "standard stop_worker mechanism."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
)


_TASK_BLOCK_SCHEMA = ToolSchema(
    name="task_block",
    description=(
        "Pause your own task and wait for additional context from your "
        "parent agent or a human. Only available when the harness has "
        "spawned you for a task (the dispatcher sets the gating "
        "automatically). Provide a one-sentence reason naming the "
        "specific decision you need; deeper context belongs in your "
        "ongoing reasoning. Does NOT consume a retry attempt — blocking "
        "is a deliberate pause, not a failure."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {"type": "string"},
        },
        "required": ["reason"],
        "additionalProperties": False,
    },
)


# ---------------------------------------------------------------------------
# spawn_task handler
# ---------------------------------------------------------------------------


async def _spawn_task_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    """Create a Task row; eagerly spawn the child Session if no parents
    are pending (atomic claim via FOR UPDATE SKIP LOCKED so we don't
    race the dispatcher tick).
    """
    session_store = kwargs.get("session_store")
    redis = kwargs.get("redis")
    tenant = kwargs.get("tenant")
    session_id_str = kwargs.get("session_id")
    session_factory = kwargs.get("session_factory")

    if not all([session_store, redis, tenant, session_id_str, session_factory]):
        return _tool_error("required harness context not available")

    goal = arguments.get("goal")
    if not goal or not str(goal).strip():
        return _tool_error("goal is required")
    goal_clean = str(goal).strip()

    context = arguments.get("context")
    if context is not None:
        context = str(context)

    agent_def_name = arguments.get("agent_type")
    if agent_def_name is not None:
        agent_def_name = str(agent_def_name).strip() or None

    max_attempts = arguments.get("max_attempts")
    if max_attempts is None:
        max_attempts = 3
    else:
        try:
            max_attempts = int(max_attempts)
        except (TypeError, ValueError):
            return _tool_error("max_attempts must be an integer")
        if max_attempts < 1:
            return _tool_error("max_attempts must be >= 1")

    parent_ids, err = _parse_uuid_list(arguments.get("parents"), field_name="parents")
    if err:
        return _tool_error(err)

    try:
        parent_session_id = UUID(str(session_id_str))
    except (ValueError, TypeError):
        return _tool_error("invalid calling session id (internal harness bug)")

    org_id: UUID = tenant.org_id

    # ----- Phase 1: validate parents + insert Task row + links --------------
    try:
        async with session_factory() as db:
            initial_status = "ready"
            if parent_ids:
                parent_rows = (
                    await db.execute(
                        select(Task).where(Task.id.in_(parent_ids))
                    )
                ).scalars().all()
                if len(parent_rows) != len(parent_ids):
                    found = {p.id for p in parent_rows}
                    missing = [pid for pid in parent_ids if pid not in found]
                    return _tool_error(
                        f"parent task(s) not found: {', '.join(str(m) for m in missing)}"
                    )
                for p in parent_rows:
                    if p.org_id != org_id:
                        return _tool_error(
                            f"parent task {p.id} belongs to a different org; "
                            "cross-org dependencies are not allowed"
                        )
                if any(p.status != "done" for p in parent_rows):
                    initial_status = "todo"

            task = Task(
                org_id=org_id,
                parent_session_id=parent_session_id,
                agent_def_name=agent_def_name,
                goal=goal_clean,
                context=context,
                status=initial_status,
                max_attempts=max_attempts,
            )
            db.add(task)
            await db.flush()
            for pid in parent_ids:
                db.add(TaskLink(parent_id=pid, child_id=task.id))
            await db.commit()
            task_id = task.id
    except Exception:
        logger.exception("spawn_task: failed to insert Task row")
        return _tool_error("internal error creating task")

    # If status is 'todo' we're done — the dispatcher tick will promote
    # and spawn once parents complete.
    if initial_status == "todo":
        return json.dumps({"task_id": str(task_id), "status": "todo"})

    # ----- Phase 2: atomic claim via UPDATE ... RETURNING -------------------
    # We can't use SELECT ... FOR UPDATE here: the FOR UPDATE row lock on
    # the tasks row blocks the FK validation lock (FOR KEY SHARE) that
    # the subsequent INSERT into sessions(task_id=...) would need to
    # acquire, deadlocking the eager-spawn path. UPDATE ... RETURNING
    # serialises against concurrent UPDATEs (the dispatcher tick uses
    # the same WHERE status='ready' guard) without holding a lock past
    # the statement, so the child Session INSERT runs unblocked.
    try:
        async with session_factory() as db:
            claim_result = await db.execute(
                update(Task)
                .where(Task.id == task_id, Task.status == "ready")
                .values(
                    status="running",
                    attempt_count=Task.attempt_count + 1,
                    started_at=func.coalesce(Task.started_at, func.now()),
                )
                .returning(Task)
                .execution_options(synchronize_session=False)
            )
            claimed = claim_result.scalar_one_or_none()
            await db.commit()

        if claimed is None:
            # Lost the race to the dispatcher tick — that's fine, the
            # work is already in-flight or imminently will be.
            return json.dumps({"task_id": str(task_id), "status": "ready"})
    except Exception:
        logger.exception("spawn_task: atomic claim failed for task %s", task_id)
        return _tool_error("internal error during atomic claim")

    # We own the task. Spawn outside any DB transaction so the child
    # Session INSERT's FK lookup against tasks(id) doesn't conflict.
    from surogates.tasks.spawn import _create_session_for_task

    try:
        child = await _create_session_for_task(
            claimed,
            session_store=session_store,
            session_factory=session_factory,
            tenant=tenant,
        )
    except ValueError as exc:
        # Roll back the claim so the tick can retry once a human fixes
        # the AgentDef catalog. Decrement attempt_count because this
        # attempt never actually ran — counting it would burn a retry
        # budget on a config error.
        try:
            async with session_factory() as db:
                await db.execute(
                    update(Task)
                    .where(Task.id == task_id)
                    .values(
                        status="ready",
                        attempt_count=Task.attempt_count - 1,
                    )
                )
                await db.commit()
        except Exception:
            logger.exception(
                "spawn_task: failed to roll back claim for task %s after "
                "spawn error; manual intervention may be required",
                task_id,
            )
        logger.warning(
            "spawn_task: agent_def_name resolution failed for task %s: %s",
            task_id, exc,
        )
        return _tool_error(str(exc))
    except Exception:
        # Unknown error during spawn: same rollback pattern.
        logger.exception("spawn_task: unexpected error during child spawn")
        try:
            async with session_factory() as db:
                await db.execute(
                    update(Task)
                    .where(Task.id == task_id)
                    .values(
                        status="ready",
                        attempt_count=Task.attempt_count - 1,
                    )
                )
                await db.commit()
        except Exception:
            logger.exception(
                "spawn_task: rollback failed for task %s; status may be "
                "stuck at 'running' with no current_session_id",
                task_id,
            )
        return _tool_error("internal error during child spawn (tick will retry)")

    # Wire the child session id back onto the Task row.
    try:
        async with session_factory() as db:
            await db.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(current_session_id=child.id)
            )
            await db.commit()
    except Exception:
        # The child Session exists but the Task row doesn't point at it.
        # The tick's finalize step will observe the orphan on the next
        # pass and recover by either matching the session or treating
        # the running attempt as crashed. Log and continue so the LLM
        # still gets a useful tool result.
        logger.exception(
            "spawn_task: failed to wire current_session_id for task %s "
            "(child %s spawned but pointer not committed)",
            task_id, child.id,
        )

    await enqueue_session(redis, child.agent_id, child.id)
    return json.dumps({
        "task_id": str(task_id),
        "status": "running",
        "worker_id": str(child.id),
    })


# ---------------------------------------------------------------------------
# unblock_task / cancel_task / task_block handlers (implemented in Tasks 5-6)
# ---------------------------------------------------------------------------


async def _unblock_task_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    """Resume a blocked task. Ownership-checked: only the spawning parent
    session may unblock its own children.

    ``additional_context`` is appended (not replaced) to ``task.context``
    with an ISO-8601 timestamp marker so subsequent attempts see the
    accumulated context history.
    """
    session_factory = kwargs.get("session_factory")
    session_id_str = kwargs.get("session_id")
    if not session_factory or not session_id_str:
        return _tool_error("required harness context not available")

    task_id_str = arguments.get("task_id")
    if not task_id_str:
        return _tool_error("task_id is required")
    try:
        task_id = UUID(str(task_id_str))
    except (ValueError, TypeError):
        return _tool_error(f"invalid task_id: {task_id_str!r}")

    try:
        parent_session_id = UUID(str(session_id_str))
    except (ValueError, TypeError):
        return _tool_error("invalid calling session id (internal harness bug)")

    additional = arguments.get("additional_context")
    if additional is not None:
        additional = str(additional).strip() or None

    async with session_factory() as db:
        task = await db.scalar(
            select(Task).where(Task.id == task_id).with_for_update()
        )
        if task is None:
            return _tool_error(f"task {task_id} not found")
        if task.parent_session_id != parent_session_id:
            return _tool_error(
                "can only unblock tasks you spawned in this session",
            )
        if task.status != "blocked":
            return _tool_error(
                f"task is not blocked (current status: {task.status})"
            )

        task.status = "ready"
        task.blocked_reason = None
        if additional:
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            prefix = task.context or ""
            sep = "\n\n" if prefix else ""
            task.context = f"{prefix}{sep}[unblock at {stamp}]\n{additional}"
        await db.commit()

    return _tool_ok(task_id=str(task_id), status="ready")


async def _cancel_task_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    """Cancel a non-terminal task. Ownership-checked. If the task is
    currently running, the in-flight Session attempt is interrupted via
    the existing ``INTERRUPT_CHANNEL_PREFIX`` pub/sub channel that
    ``stop_worker`` uses.
    """
    session_factory = kwargs.get("session_factory")
    session_id_str = kwargs.get("session_id")
    redis = kwargs.get("redis")
    if not session_factory or not session_id_str or not redis:
        return _tool_error("required harness context not available")

    task_id_str = arguments.get("task_id")
    if not task_id_str:
        return _tool_error("task_id is required")
    try:
        task_id = UUID(str(task_id_str))
    except (ValueError, TypeError):
        return _tool_error(f"invalid task_id: {task_id_str!r}")

    try:
        parent_session_id = UUID(str(session_id_str))
    except (ValueError, TypeError):
        return _tool_error("invalid calling session id (internal harness bug)")

    running_session_id: UUID | None = None
    async with session_factory() as db:
        task = await db.scalar(
            select(Task).where(Task.id == task_id).with_for_update()
        )
        if task is None:
            return _tool_error(f"task {task_id} not found")
        if task.parent_session_id != parent_session_id:
            return _tool_error(
                "can only cancel tasks you spawned in this session",
            )
        if task.status in ("done", "failed", "cancelled"):
            return _tool_error(
                f"task already terminal: {task.status}"
            )

        if task.status == "running":
            running_session_id = task.current_session_id
        task.status = "cancelled"
        task.completed_at = func.now()
        await db.commit()

    if running_session_id is not None:
        await redis.publish(
            f"{INTERRUPT_CHANNEL_PREFIX}{running_session_id}", "task_cancel",
        )

    return _tool_ok(task_id=str(task_id), status="cancelled")


async def _task_block_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    """Pause the calling Session's task without consuming a retry attempt.

    Gating: the per-session filter in
    ``surogates.orchestrator.worker._filter_effective_tools`` strips this
    tool from the schema when ``Session.task_id is None``, so the LLM
    never sees it on plain chat / spawn_worker sessions. The runtime
    check here is belt-and-suspenders for the rare case where filtering
    failed (e.g., legacy schema cache).
    """
    session_store = kwargs.get("session_store")
    redis = kwargs.get("redis")
    session_id_str = kwargs.get("session_id")
    session_factory = kwargs.get("session_factory")
    if not session_store or not redis or not session_id_str or not session_factory:
        return _tool_error("required harness context not available")

    reason = arguments.get("reason")
    if not reason or not str(reason).strip():
        return _tool_error(
            "reason is required — name the specific decision you need"
        )
    reason_clean = str(reason).strip()

    try:
        session_id = UUID(str(session_id_str))
    except (ValueError, TypeError):
        return _tool_error("invalid calling session id (internal harness bug)")

    # Resolve task_id via a fresh read of the session row so we don't
    # rely on the harness having populated a Pydantic snapshot.
    async with session_factory() as db:
        session_row = await db.get(ORMSession, session_id)
        if session_row is None:
            return _tool_error(
                f"calling session {session_id} not found (internal harness bug)"
            )
        if session_row.task_id is None:
            return _tool_error(
                "task_block is only available when running for a task"
            )
        task_id = session_row.task_id

    parent_session_id: UUID
    async with session_factory() as db:
        task = await db.scalar(
            select(Task).where(Task.id == task_id).with_for_update()
        )
        if task is None:
            return _tool_error(
                f"task {task_id} not found (was the row deleted mid-run?)"
            )
        if task.current_session_id != session_id:
            return _tool_error(
                "this attempt is no longer the current task attempt "
                "(likely reclaimed by stale-claim recovery)"
            )
        if task.status != "running":
            return _tool_error(
                f"task is not running (current status: {task.status})"
            )

        task.status = "blocked"
        task.blocked_reason = reason_clean
        parent_session_id = task.parent_session_id
        await db.commit()

    # Tell the parent so its next wake sees the block as inbox-equivalent.
    from surogates.session.events import EventType

    await session_store.emit_event(
        parent_session_id,
        EventType.TASK_BLOCKED,
        {
            "task_id": str(task_id),
            "worker_id": str(session_id),
            "reason": reason_clean,
        },
    )
    # Cleanly terminate this Session via the same interrupt mechanism
    # stop_worker uses; the harness loop exits between iterations.
    await redis.publish(
        f"{INTERRUPT_CHANNEL_PREFIX}{session_id}", "task_block",
    )
    return _tool_ok(task_id=str(task_id), status="blocked")


# ---------------------------------------------------------------------------
# Registration (called from surogates.tools.builtin.__init__ at boot)
# ---------------------------------------------------------------------------


def register(registry: ToolRegistry) -> None:
    """Register the four task-layer tools into ``registry``.

    Idempotent within a process: the ToolRegistry rejects duplicate
    registrations with ``ValueError``, so this should be called exactly
    once per registry instance.
    """
    registry.register(
        name="spawn_task",
        schema=_SPAWN_TASK_SCHEMA,
        handler=_spawn_task_handler,
        toolset="core",
    )
    registry.register(
        name="unblock_task",
        schema=_UNBLOCK_TASK_SCHEMA,
        handler=_unblock_task_handler,
        toolset="core",
    )
    registry.register(
        name="cancel_task",
        schema=_CANCEL_TASK_SCHEMA,
        handler=_cancel_task_handler,
        toolset="core",
    )
    registry.register(
        name="task_block",
        schema=_TASK_BLOCK_SCHEMA,
        handler=_task_block_handler,
        toolset="core",
    )
