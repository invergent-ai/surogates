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

Hosted by the orchestrator's main loop at a 5-second cadence.
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
        outcome, last_event = classify_attempt_outcome(
            events, session_status=sess.status,
        )

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
    file_bundle_cache: Any | None = None,
) -> int:
    """Atomically claim up to ``_MAX_ENQUEUES_PER_TICK`` ready tasks,
    create child Sessions, push to the Redis work queue.

    Each iteration runs in its own short transaction. The claim uses
    ``UPDATE ... RETURNING`` (NOT ``SELECT FOR UPDATE``) to avoid the
    same deadlock pattern the spawn_task tool sidesteps: the FOR UPDATE
    lock on the task row would block the subsequent FK validation lock
    that ``create_child_session``'s INSERT into ``sessions`` acquires.
    """
    from surogates.tasks.spawn import (
        UnknownAgentDefError,
        _create_session_for_task,
    )

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
            await _retry_or_block(
                session_factory, claimed, reason="tenant resolution failed",
            )
            continue

        try:
            child = await _create_session_for_task(
                claimed,
                session_store=session_store,
                session_factory=session_factory,
                tenant=tenant,
                file_bundle_cache=file_bundle_cache,
            )
        except UnknownAgentDefError as exc:
            # Permanent config error: the referenced agent_def does not exist
            # in the catalog / bundle, so retrying can never succeed. Block the
            # task (a first-class "needs attention" state surfaced in the UI,
            # self-heals via unblock_task) instead of rolling it back to
            # 'ready' and re-attempting every tick forever — the latter spammed
            # the worker log indefinitely for a single misconfigured task.
            logger.warning(
                "tasks_tick: blocking task %s (unknown agent_def): %s",
                claimed.id, exc,
            )
            failed_this_tick.add(claimed.id)
            await _block_claim(session_factory, claimed.id, reason=str(exc))
            continue
        except ValueError as exc:
            # Permanent config error, same class as UnknownAgentDefError above:
            # the raise sites are shape checks on the PARENT session's config
            # (missing storage_bucket / storage_key_prefix / workspace_path),
            # which is a property of a row that never changes on its own.
            # Rolling this back returned the task to 'ready' with its attempt
            # given back, so the tick re-claimed it every 5s forever — the
            # ceiling in _finalize_ended_attempts only inspects 'running'
            # tasks and could never see it.
            logger.warning(
                "tasks_tick: blocking task %s (spawn cannot succeed): %s",
                claimed.id, exc,
            )
            failed_this_tick.add(claimed.id)
            await _block_claim(session_factory, claimed.id, reason=str(exc))
            continue
        except Exception as exc:
            logger.exception(
                "tasks_tick: unexpected error spawning task %s",
                claimed.id,
            )
            failed_this_tick.add(claimed.id)
            await _retry_or_block(
                session_factory, claimed, reason=f"spawn failed: {exc}",
            )
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


async def _block_claim(
    session_factory: async_sessionmaker, task_id: UUID, *, reason: str,
) -> None:
    """Best-effort: move a claimed task to ``blocked`` with ``blocked_reason``
    (a permanent config error — the referenced agent_def does not exist).

    Decrements ``attempt_count`` to undo the claim's increment so the block
    doesn't burn a retry budget: once the catalog is fixed and the task is
    unblocked (``unblock_task`` → ``ready``), it gets a clean attempt. Unlike
    ``_rollback_claim`` this leaves the task OUT of the ``ready`` pool, so the
    dispatcher stops re-attempting (and re-logging) it every tick.
    """
    try:
        async with session_factory() as db:
            await db.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(
                    status="blocked",
                    blocked_reason=reason[:500],
                    attempt_count=Task.attempt_count - 1,
                )
            )
            await db.commit()
    except Exception:
        logger.exception(
            "tasks_tick: block for task %s failed; finalize step will "
            "recover by treating the attempt as crashed",
            task_id,
        )


async def _retry_or_block(
    session_factory: async_sessionmaker, claimed: Task, *, reason: str,
) -> None:
    """Return a failed claim to ``ready``, or block it once attempts are spent.

    ``claimed.attempt_count`` already includes the attempt just consumed (the
    claim increments it), so this is the only place a repeatedly-failing spawn
    can be stopped: ``_finalize_ended_attempts``' ceiling only inspects tasks
    in ``running`` joined to an ended Session, and a task returned to ``ready``
    is never seen by it.
    """
    if claimed.attempt_count >= claimed.max_attempts:
        logger.warning(
            "tasks_tick: blocking task %s after %d/%d failed spawn attempts: %s",
            claimed.id, claimed.attempt_count, claimed.max_attempts, reason,
        )
        await _block_claim(session_factory, claimed.id, reason=reason)
        return
    await _rollback_claim(session_factory, claimed.id)


async def _rollback_claim(
    session_factory: async_sessionmaker, task_id: UUID,
) -> None:
    """Best-effort: roll a failed claim back to ``ready``.

    The consumed attempt is deliberately **kept**.  Giving it back restored
    the row to its exact pre-claim state, which made ``max_attempts``
    unenforceable and let a task whose spawn could never succeed be
    re-claimed on every tick, forever.

    Errors during the rollback itself are logged but not raised — the
    next finalize pass will recover by treating the running attempt as
    crashed.
    """
    try:
        async with session_factory() as db:
            await db.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(status="ready")
            )
            await db.commit()
    except Exception:
        logger.exception(
            "tasks_tick: rollback for task %s failed; finalize step will "
            "recover by treating the attempt as crashed",
            task_id,
        )



# How long a mission's coordinator may be idle before the tick re-drives it.
# Generous on purpose: poking a coordinator mid-thought is worse than waiting,
# and the sweep only exists to rescue objectives nothing else will touch.
_MISSION_IDLE_SECONDS: int = 900

# Nudges without an intervening evaluation before the mission is handed to a
# human. Matches the orphan-recovery ceiling and the parse-failure pause, so an
# operator meets one number rather than three.
_MAX_MISSION_NUDGES: int = 3


async def nudge_idle_missions(
    *,
    session_factory: async_sessionmaker,
    session_store: Any,
    redis: Any,
    idle_seconds: int = _MISSION_IDLE_SECONDS,
) -> int:
    """Re-drive active missions whose coordinator has gone quiet.

    ``should_evaluate`` only runs inside a coordinator turn, and a text-only
    LLM response ends that turn without re-enqueueing anything. A mission
    whose coordinator stops talking is therefore unreachable: no evaluation,
    no budget check, no stagnation count — every other mission gate is
    downstream of a loop nothing was driving.

    Skips a mission whose coordinator currently holds a lease: a worker is on
    it right now and interrupting mid-thought is worse than waiting.

    Bounded. Nudges since the mission last progressed — its last evaluation,
    or its creation if it has never been evaluated — are counted, and past
    :data:`_MAX_MISSION_NUDGES` the mission is blocked for a human instead of
    being poked forever. An evaluation resets the count, because that means
    the loop moved.
    """
    from surogates.config import enqueue_session
    from surogates.db.models import Mission as MissionRow
    from surogates.missions.store import MissionStore
    from surogates.session.events import EventType

    # Compared by the database clock, which is the one that stamped
    # ``updated_at`` — these columns are naive, so a Python-side aware
    # datetime would not even encode.
    async with session_factory() as db:
        rows = (await db.execute(
            select(MissionRow).where(
                MissionRow.status == "active",
                MissionRow.updated_at
                < func.now() - text(f"make_interval(secs => {int(idle_seconds)})"),
            ).limit(_MAX_ENQUEUES_PER_TICK)
        )).scalars().all()
        missions = [
            (m.id, m.session_id, m.org_id, m.agent_id, m.description,
             m.last_evaluation_at, m.created_at)
            for m in rows
        ]

    store = MissionStore(session_factory)
    woken = 0
    for mid, sid, org_id, agent_id, description, last_eval, created in missions:
        try:
            if await session_store.has_live_lease(sid):
                continue

            since = last_eval or created
            nudges = await session_store.count_synthetic_since(
                sid, synthetic="mission_nudge", since=since,
            )
            if nudges >= _MAX_MISSION_NUDGES:
                logger.warning(
                    "mission %s nudged %d times with no evaluation; blocking",
                    mid, nudges,
                )
                await store.set_status(
                    mid, "blocked",
                    paused_reason=(
                        f"coordinator went quiet after {nudges} nudges "
                        "without an evaluation"
                    ),
                )
                continue

            await session_store.emit_event(
                sid, EventType.USER_MESSAGE,
                {
                    "content": (
                        "[Mission still open]\n\n"
                        f"Objective: {description}\n\n"
                        "Your last turn ended without completing this mission "
                        "and nothing has advanced it since. Continue the work: "
                        "spawn the next task, or if the objective is genuinely "
                        "finished say so and mark it complete."
                    ),
                    "synthetic": "mission_nudge",
                    "mission_id": str(mid),
                },
            )
            await enqueue_session(
                redis, org_id=str(org_id), agent_id=agent_id, session_id=sid,
            )
            woken += 1
        except Exception:
            logger.exception("mission liveness nudge failed for %s", mid)

    return woken


async def tasks_tick(
    *,
    session_factory: async_sessionmaker,
    redis: Any,
    session_store: Any,
    tenant_for_task: Callable[[Any], Any],
    file_bundle_cache: Any | None = None,
) -> dict[str, int]:
    """Run one tick of the subagent task layer.

    Returns ``{"promoted": int, "finalized": int, "enqueued": int,
    "nudged": int}`` for
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
        file_bundle_cache=file_bundle_cache,
    )
    # Re-drive objectives nothing else will touch. Runs last: the steps
    # above may have produced the very progress that makes a mission look
    # alive again.
    nudged = await nudge_idle_missions(
        session_factory=session_factory,
        session_store=session_store,
        redis=redis,
    )
    return {
        "promoted": promoted, "finalized": finalized,
        "enqueued": enqueued, "nudged": nudged,
    }
