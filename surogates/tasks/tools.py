"""Subagent task layer tools.

Public surface (all registered via :func:`register`):

* ``spawn_task``     — create a durable subagent task with optional DAG
                       parents and ``max_attempts``; eagerly spawns when
                       no parents are pending.
* ``unblock_task``   — orchestrator-only; resume a blocked task with
                       optional additional context.
* ``cancel_task``    — orchestrator-only; abort a non-terminal task.
* ``worker_block``   — self-tool gated on ``Session.task_id``; pauses the
                       current attempt without consuming a retry.
* ``worker_complete``— self-tool gated on ``Session.task_id``; emits a
                       structured handoff to the spawning parent.
* ``worker_context`` — self-tool gated on ``Session.task_id``; reads the
                       calling worker's task context (goal, parents,
                       prior attempts).

All are registered into the ``"core"`` toolset. Per-session gating (the
``worker_*`` self-tools are only visible when ``Session.task_id is not
None``) lives in
``surogates.orchestrator.worker._filter_effective_tools`` to match
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
        "via worker_block + unblock_task. Use this when work must outlive "
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


_WORKER_COMPLETE_SCHEMA = ToolSchema(
    name="worker_complete",
    description=(
        "Worker self-tool: mark your own subagent execution attempt as "
        "done with a structured handoff to your spawning parent. "
        "Available only when the harness has spawned you for a task — "
        "NOT a tool for signalling that a user's chat request is "
        "finished. Prefer this over letting the harness emit "
        "WORKER_COMPLETE implicitly when you want to give the parent "
        "agent (or future retries) machine-readable structured output: "
        "changed_files, tests_run, decisions, findings, etc. The "
        "``summary`` is a 1-3 sentence human-readable description; "
        "``metadata`` is a free-form JSON object."
    ),
    parameters={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "1-3 sentence human-readable handoff. Becomes "
                    "``task.result`` and appears in the WORKER_COMPLETE "
                    "event delivered to the spawning parent agent."
                ),
            },
            "metadata": {
                "type": "object",
                "description": (
                    "Optional structured handoff. Free-form JSON; "
                    "common keys: ``changed_files``, ``tests_run``, "
                    "``tests_passed``, ``decisions``, ``findings``, "
                    "``approved``."
                ),
            },
        },
        "required": ["summary"],
        "additionalProperties": False,
    },
)


_WORKER_CONTEXT_SCHEMA = ToolSchema(
    name="worker_context",
    description=(
        "Worker self-tool: read your own execution attempt's full "
        "context — goal, accumulated context from your parent, parent "
        "tasks (with their results), and prior attempts of THIS task "
        "(with summaries / errors / outcomes). Available only when the "
        "harness has spawned you for a task — NOT a tool for inspecting "
        "the user's chat request. Useful on retry to see why earlier "
        "attempts failed and what they produced before failing — don't "
        "repeat their mistakes."
    ),
    parameters={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
)


_WORKER_BLOCK_SCHEMA = ToolSchema(
    name="worker_block",
    description=(
        "Worker self-tool: pause your own subagent execution attempt "
        "and wait for additional context from your spawning parent or "
        "a human. Available only when the harness has spawned you for "
        "a task (the dispatcher sets the gating automatically) — NOT a "
        "tool for pausing the user's chat. Provide a one-sentence "
        "reason naming the specific decision you need; deeper context "
        "belongs in your ongoing reasoning. Does NOT consume a retry "
        "attempt — blocking is a deliberate pause, not a failure."
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

    # ----- Phase 0: read the calling session's active_mission_id so the
    # spawned Task inherits the mission scope.  The session row is the
    # source of truth (matches /mission's writes); reading config from a
    # stale in-memory Session object would miss missions created after
    # the harness loaded its snapshot.
    active_mission_id: UUID | None = None
    try:
        async with session_factory() as db:
            sess_row = await db.get(ORMSession, parent_session_id)
            if sess_row is not None:
                raw = (sess_row.config or {}).get("active_mission_id")
                if raw:
                    try:
                        active_mission_id = UUID(str(raw))
                    except (ValueError, TypeError):
                        logger.warning(
                            "spawn_task: session %s has malformed active_mission_id=%r; ignoring",
                            parent_session_id, raw,
                        )
    except Exception:
        logger.exception(
            "spawn_task: failed to read active_mission_id for session %s",
            parent_session_id,
        )

    # ----- Phases 1-2 + spawn + enqueue are shared with dispatch_experiments
    # (research missions) via surogates.tasks.service.create_task_and_spawn.
    from surogates.tasks.service import TaskSpawnError, create_task_and_spawn

    try:
        result = await create_task_and_spawn(
            goal=goal_clean,
            context=context,
            agent_def_name=agent_def_name,
            max_attempts=max_attempts,
            parent_ids=parent_ids,
            parent_session_id=parent_session_id,
            org_id=org_id,
            mission_id=active_mission_id,
            session_store=session_store,
            session_factory=session_factory,
            redis=redis,
            tenant=tenant,
        )
    except TaskSpawnError as exc:
        return _tool_error(str(exc))
    return json.dumps(result)


# ---------------------------------------------------------------------------
# unblock_task / cancel_task / worker_block handlers (implemented in Tasks 5-6)
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


async def _worker_complete_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    """Worker explicitly marks its own task done with a structured handoff.

    Gating: per-session filter strips this from the schema when
    ``Session.task_id is None``.  Runtime check is belt-and-suspenders.

    The handler updates the Task row to terminal ``done`` state and
    publishes an interrupt on the worker's own Redis channel so the
    harness loop exits between iterations.  The harness's natural
    session-end path then runs ``notify_parent_on_completion``, which
    reads ``task.result``/``task.result_metadata`` from the row and
    delivers them to the parent in the ``WORKER_COMPLETE`` event
    payload — so parents see the explicit summary, not the LLM's last
    response (see :func:`surogates.harness.worker_notify.notify_parent_on_completion`).
    """
    session_factory = kwargs.get("session_factory")
    session_id_str = kwargs.get("session_id")
    redis = kwargs.get("redis")
    if not session_factory or not session_id_str or not redis:
        return _tool_error("required harness context not available")

    summary = arguments.get("summary")
    if not summary or not str(summary).strip():
        return _tool_error("summary is required")
    summary_clean = str(summary).strip()

    metadata = arguments.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        return _tool_error(
            f"metadata must be a JSON object, got {type(metadata).__name__}"
        )

    try:
        session_id = UUID(str(session_id_str))
    except (ValueError, TypeError):
        return _tool_error("invalid calling session id (internal harness bug)")

    async with session_factory() as db:
        session_row = await db.get(ORMSession, session_id)
        if session_row is None:
            return _tool_error(
                f"calling session {session_id} not found (internal harness bug)"
            )
        if session_row.task_id is None:
            return _tool_error(
                "worker_complete is only available when running for a task "
                "(this is a worker self-tool, not a chat-completion signal)"
            )
        task_id = session_row.task_id

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

        task.status = "done"
        task.result = summary_clean
        task.result_metadata = metadata
        task.completed_at = func.now()
        await db.commit()

    # Interrupt this session so the harness loop exits cleanly. The
    # harness's normal session-end path then emits WORKER_COMPLETE
    # carrying our explicit summary + metadata via the override in
    # notify_parent_on_completion.
    await redis.publish(
        f"{INTERRUPT_CHANNEL_PREFIX}{session_id}", "worker_complete",
    )
    return _tool_ok(task_id=str(task_id), status="done")


async def _worker_context_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    """Return the calling worker's full task context.

    Includes: the task row itself, parent tasks with their completed
    results, and prior attempt summaries for this same task (from
    sessions linked via ``sessions.task_id``).  Workers use this on
    retry to understand why earlier attempts failed and what they
    produced — the new attempt's initial USER_MESSAGE includes a brief
    summary already, but ``worker_context`` exposes the full detail when
    needed.
    """
    session_factory = kwargs.get("session_factory")
    session_store = kwargs.get("session_store")
    session_id_str = kwargs.get("session_id")
    if not session_factory or not session_store or not session_id_str:
        return _tool_error("required harness context not available")

    try:
        session_id = UUID(str(session_id_str))
    except (ValueError, TypeError):
        return _tool_error("invalid calling session id (internal harness bug)")

    async with session_factory() as db:
        session_row = await db.get(ORMSession, session_id)
        if session_row is None or session_row.task_id is None:
            return _tool_error(
                "worker_context is only available when running for a task "
                "(this is a worker self-tool, not a chat-context inspector)"
            )
        task_id = session_row.task_id
        task = await db.get(Task, task_id)
        if task is None:
            return _tool_error(f"task {task_id} not found")

        # Parents: read each parent task's status, goal, and result.
        from surogates.db.models import TaskLink as _TaskLink
        parent_link_rows = (await db.execute(
            select(_TaskLink).where(_TaskLink.child_id == task_id)
        )).scalars().all()
        parent_ids = [row.parent_id for row in parent_link_rows]
        parents_payload: list[dict[str, Any]] = []
        if parent_ids:
            parent_rows = (await db.execute(
                select(Task).where(Task.id.in_(parent_ids))
            )).scalars().all()
            for p in parent_rows:
                parents_payload.append({
                    "id": str(p.id),
                    "goal": p.goal,
                    "status": p.status,
                    "result": p.result,
                    "result_metadata": p.result_metadata,
                })

        # Prior attempts: sessions for this task other than the current
        # one, ordered by created_at (earliest first).
        prior_sessions = (await db.execute(
            select(ORMSession)
            .where(ORMSession.task_id == task_id)
            .where(ORMSession.id != session_id)
            .order_by(ORMSession.created_at)
        )).scalars().all()

    prior_payload: list[dict[str, Any]] = []
    for sess in prior_sessions:
        try:
            events = await session_store.get_events(sess.id)
        except Exception:
            events = []
        from surogates.tasks.completion import (
            classify_attempt_outcome,
            extract_result_from_completion_event,
        )
        outcome, last_event = classify_attempt_outcome(events)
        entry: dict[str, Any] = {
            "session_id": str(sess.id),
            "outcome": outcome.value,
        }
        if outcome.value == "completed" and last_event is not None:
            entry["result"] = extract_result_from_completion_event(last_event)
        elif outcome.value == "blocked" and last_event is not None:
            raw = getattr(last_event, "payload", None) or getattr(last_event, "data", None) or {}
            if isinstance(raw, str):
                import json as _json
                try:
                    raw = _json.loads(raw)
                except Exception:
                    raw = {}
            entry["blocked_reason"] = (raw or {}).get("reason")
        prior_payload.append(entry)

    return json.dumps({
        "task": {
            "id": str(task.id),
            "goal": task.goal,
            "context": task.context,
            "status": task.status,
            "attempt_count": task.attempt_count,
            "max_attempts": task.max_attempts,
            "agent_def_name": task.agent_def_name,
            "blocked_reason": task.blocked_reason,
        },
        "parents": parents_payload,
        "prior_attempts": prior_payload,
    })


async def _worker_block_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
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
                "worker_block is only available when running for a task "
                "(this is a worker self-tool, not a chat-pause signal)"
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
        f"{INTERRUPT_CHANNEL_PREFIX}{session_id}", "worker_block",
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
        name="worker_block",
        schema=_WORKER_BLOCK_SCHEMA,
        handler=_worker_block_handler,
        toolset="core",
    )
    registry.register(
        name="worker_complete",
        schema=_WORKER_COMPLETE_SCHEMA,
        handler=_worker_complete_handler,
        toolset="core",
    )
    registry.register(
        name="worker_context",
        schema=_WORKER_CONTEXT_SCHEMA,
        handler=_worker_context_handler,
        toolset="core",
    )
