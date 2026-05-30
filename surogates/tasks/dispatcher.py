"""Subagent task layer dispatcher tick.

One ``tasks_tick()`` invocation runs three SQL-driven steps:

1. **Promote** — ``UPDATE tasks SET status='ready' WHERE status='todo'
   AND every parent is done``.  Cancelled / failed parents do NOT
   unblock children; orchestrators must explicitly cancel/replan.
2. **Finalize** — for each Task in ``running`` whose
   ``current_session_id`` Session has ended, classify the attempt via
   ``surogates.tasks.completion.classify_attempt_outcome`` and write
   the resulting terminal (or retry) status.
3. **Enqueue** — atomically claim up to ``_MAX_ENQUEUES_PER_TICK`` Tasks
   in ``ready`` via ``UPDATE ... RETURNING`` (avoids the deadlock
   between SELECT FOR UPDATE and the child Session INSERT's FK lock —
   see ``surogates.tasks.tools._spawn_task_handler`` for the rationale),
   create a child Session via the factored
   ``_create_session_for_task`` primitive, and push the Session id onto
   the agent's Redis work queue.

Hosted by the orchestrator's main loop at a 5-second cadence (Task 10).
The function is idempotent under concurrent invocation — the ``UPDATE
... WHERE status=X`` guards plus ``SKIP LOCKED`` semantics ensure each
row is claimed by at most one tick.
"""
from __future__ import annotations

import logging
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from surogates.config import enqueue_session
from surogates.db.models import Session as ORMSession, Task
from surogates.session.events import EventType
from surogates.tasks.completion import (
    TaskAttemptOutcome,
    classify_attempt_outcome,
    extract_result_from_completion_event,
)

logger = logging.getLogger(__name__)


# Maximum number of ``ready`` tasks the tick will enqueue per pass.  A
# flood of newly-promoted tasks won't monopolise a single tick; they
# bleed off across the next few 5s windows.
_MAX_ENQUEUES_PER_TICK: int = 10


# Session statuses that mean "this session is no longer running" for the
# purposes of finalising a Task attempt. ``paused`` is included for
# completeness — a paused task-backed session is effectively ended from
# the task layer's perspective (the agent isn't producing output), and
# the tick should classify it the same way as a crash.
_ENDED_SESSION_STATUSES: tuple[str, ...] = ("completed", "failed", "cancelled", "paused")


_PROMOTE_SQL = text(
    """
    UPDATE tasks SET status = 'ready'
     WHERE status = 'todo'
       AND NOT EXISTS (
         SELECT 1 FROM task_links tl
         JOIN tasks p ON p.id = tl.parent_id
         WHERE tl.child_id = tasks.id
           AND p.status != 'done'
       )
    """
)


async def _promote_todo_to_ready(db) -> int:
    """Bulk-promote ``todo`` Tasks whose every parent has reached ``done``.

    Returns the number of rows promoted.  Cancelled / failed parents
    intentionally do NOT promote children — see ``Task.status`` docstring.
    """
    result = await db.execute(_PROMOTE_SQL)
    return result.rowcount or 0


async def _finalize_ended_sessions(
    db,
    *,
    session_store: Any,
) -> int:
    """Walk every running Task whose Session ended, finalise its status.

    Returns the number of Tasks transitioned.  Three outcomes per row,
    classified by ``classify_attempt_outcome``:

    * COMPLETED → ``status='done'``, ``result`` from event payload
    * BLOCKED   → already handled by the ``worker_block`` tool; no-op
    * CRASHED   → retry (``status='ready'``) if attempts remain, else
                  ``status='failed'`` + ``TASK_FAILED`` event to parent
    """
    rows = (await db.execute(
        select(Task, ORMSession)
        .join(ORMSession, ORMSession.id == Task.current_session_id)
        .where(Task.status == "running")
        .where(ORMSession.status.in_(_ENDED_SESSION_STATUSES))
    )).all()

    finalized = 0
    failed_to_emit: list[tuple[UUID, UUID, int]] = []
    for task, sess in rows:
        events = await session_store.get_events(sess.id)
        outcome, last_event = classify_attempt_outcome(events)

        if outcome is TaskAttemptOutcome.COMPLETED:
            task.status = "done"
            task.result = extract_result_from_completion_event(last_event)
            task.completed_at = func.now()
            finalized += 1
        elif outcome is TaskAttemptOutcome.BLOCKED:
            # The worker_block tool already wrote status='blocked' inside
            # its own txn before publishing the interrupt that stopped
            # this Session. The WHERE clause should have filtered this
            # task out — if we got here it means a race: read the
            # 'running' row before the tool committed. Skip; the next
            # tick will see the now-blocked task and skip too.
            continue
        else:
            # CRASHED — no terminal-attempt event present.
            if task.attempt_count >= task.max_attempts:
                task.status = "failed"
                task.completed_at = func.now()
                failed_to_emit.append((
                    task.parent_session_id, task.id, task.attempt_count,
                ))
            else:
                task.status = "ready"
            finalized += 1

    await db.commit()

    # Emit TASK_FAILED events outside the txn so a slow event-write
    # doesn't hold the row lock open.
    for parent_session_id, task_id, attempt_count in failed_to_emit:
        try:
            await session_store.emit_event(
                parent_session_id,
                EventType.TASK_FAILED,
                {
                    "task_id": str(task_id),
                    "attempt_count": attempt_count,
                },
            )
        except Exception:
            logger.exception(
                "tasks_tick: failed to emit TASK_FAILED for task %s on "
                "parent session %s; parent agent will not see the failure "
                "until it re-reads the task row",
                task_id, parent_session_id,
            )

    return finalized


async def _enqueue_ready_tasks(
    *,
    session_factory: async_sessionmaker,
    redis: Any,
    session_store: Any,
    tenant_for_task: Callable[[Any], Any],
) -> int:
    """Atomically claim up to ``_MAX_ENQUEUES_PER_TICK`` ready tasks,
    create child Sessions, push to the Redis work queue.

    Each iteration runs in its own short transaction. The claim uses
    ``UPDATE ... RETURNING`` (NOT ``SELECT FOR UPDATE``) to avoid the
    same deadlock pattern the spawn_task tool sidesteps: the FOR UPDATE
    lock on the task row would block the subsequent FK validation lock
    that ``create_child_session``'s INSERT into ``sessions`` acquires.
    """
    from surogates.tasks.spawn import _create_session_for_task

    enqueued = 0
    # Tasks whose spawn raised within this tick.  Excluded from
    # subsequent iterations so a single broken task (missing AgentDef,
    # bad workspace config) can't hot-loop and starve the rest of the
    # ready queue.  A different tick will see the rolled-back row again
    # — the exclusion is per-tick, not permanent.
    failed_this_tick: set[UUID] = set()
    for _ in range(_MAX_ENQUEUES_PER_TICK):
        # Phase A: atomic claim.
        async with session_factory() as db:
            stmt = (
                update(Task)
                .where(Task.status == "ready")
                .values(
                    status="running",
                    attempt_count=Task.attempt_count + 1,
                    started_at=func.coalesce(Task.started_at, func.now()),
                )
                .returning(Task)
                .execution_options(synchronize_session=False)
            )
            # Limit to one row per iteration: Postgres doesn't natively
            # support LIMIT inside UPDATE, but a subquery achieves the
            # same effect with SKIP LOCKED semantics by id ordering.
            select_stmt = (
                select(Task.id)
                .where(Task.status == "ready")
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if failed_this_tick:
                select_stmt = select_stmt.where(~Task.id.in_(failed_this_tick))
            limited_id = await db.scalar(select_stmt)
            if limited_id is None:
                await db.rollback()
                break
            stmt = stmt.where(Task.id == limited_id)
            claimed = (await db.execute(stmt)).scalar_one_or_none()
            await db.commit()

        if claimed is None:
            # The ``ready`` task we identified was claimed by another
            # tick instance between our SELECT and our UPDATE.  Try
            # the next loop iteration; there may be more work.
            continue

        # Phase B: spawn the child Session outside any row lock.
        try:
            tenant = tenant_for_task(claimed)
        except Exception:
            logger.exception(
                "tasks_tick: tenant_for_task() raised for task %s; "
                "rolling back claim",
                claimed.id,
            )
            failed_this_tick.add(claimed.id)
            await _rollback_claim(session_factory, claimed.id)
            continue

        try:
            child = await _create_session_for_task(
                claimed,
                session_store=session_store,
                session_factory=session_factory,
                tenant=tenant,
            )
        except ValueError as exc:
            # Config-level error (unknown agent_def_name, missing
            # workspace fields on the parent session). Rolling back
            # gives a human a chance to fix the catalog / config; the
            # within-tick exclusion prevents hot-looping on this same
            # row for the rest of THIS tick.
            logger.warning(
                "tasks_tick: spawn failed for task %s: %s",
                claimed.id, exc,
            )
            failed_this_tick.add(claimed.id)
            await _rollback_claim(session_factory, claimed.id)
            continue
        except Exception:
            logger.exception(
                "tasks_tick: unexpected error spawning task %s; rolling back",
                claimed.id,
            )
            failed_this_tick.add(claimed.id)
            await _rollback_claim(session_factory, claimed.id)
            continue

        # Phase C: wire current_session_id onto the task.
        try:
            async with session_factory() as db:
                await db.execute(
                    update(Task)
                    .where(Task.id == claimed.id)
                    .values(current_session_id=child.id)
                )
                await db.commit()
        except Exception:
            logger.exception(
                "tasks_tick: failed to wire current_session_id for task %s "
                "(child %s spawned but pointer not committed)",
                claimed.id, child.id,
            )
            # Don't try to roll back — the child session exists and is
            # functional. The next finalize pass will observe the
            # orphan if necessary.

        await enqueue_session(
            redis,
            org_id=str(child.org_id),
            agent_id=child.agent_id,
            session_id=child.id,
        )
        enqueued += 1

    return enqueued


async def _rollback_claim(
    session_factory: async_sessionmaker, task_id: UUID,
) -> None:
    """Best-effort: roll a failed claim back to ``ready`` and decrement
    ``attempt_count`` so a config error doesn't burn a retry budget.

    Errors during the rollback itself are logged but not raised — the
    next finalize pass will recover by treating the running attempt as
    crashed.
    """
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
            "tasks_tick: rollback for task %s failed; finalize step will "
            "recover by treating the attempt as crashed",
            task_id,
        )


async def tasks_tick(
    *,
    session_factory: async_sessionmaker,
    redis: Any,
    session_store: Any,
    tenant_for_task: Callable[[Any], Any],
) -> dict[str, int]:
    """Run one tick of the subagent task layer.

    Returns ``{"promoted": int, "finalized": int, "enqueued": int}`` for
    observability hooks (the orchestrator metrics pipeline consumes these
    in Task 10).

    Idempotent: every step uses SQL guards (status checks +
    ``UPDATE ... RETURNING`` for claim atomicity) so two concurrent
    invocations against the same DB will at worst do no-ops on the
    "lost" side.

    ``tenant_for_task`` is a synchronous callable that takes a Task ORM
    row and returns a tenant context object (with ``.org_id`` /
    ``.user_id`` attributes).  Hosted by the orchestrator since
    Task-layer code has no per-process tenant injection of its own.
    """
    async with session_factory() as db:
        promoted = await _promote_todo_to_ready(db)
        await db.commit()
    async with session_factory() as db:
        finalized = await _finalize_ended_sessions(db, session_store=session_store)
    enqueued = await _enqueue_ready_tasks(
        session_factory=session_factory,
        redis=redis,
        session_store=session_store,
        tenant_for_task=tenant_for_task,
    )
    return {"promoted": promoted, "finalized": finalized, "enqueued": enqueued}
