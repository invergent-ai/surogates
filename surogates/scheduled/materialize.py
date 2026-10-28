"""Materialize scheduled-session runs.

The platform ticker (:mod:`surogates.scheduled.platform_ticker`) claims
due ``scheduled_sessions`` rows and hands each to
:func:`materialize_scheduled_run`, which:

1. Creates the actual run :class:`~surogates.session.models.Session`
   (a child of the schedule's creator session when one exists, so loop
   iterations share a workspace; otherwise a fresh agent session).
2. Emits the schedule's prompt as a ``USER_MESSAGE`` event.
3. Enqueues the **run session id** on the shared work queue so the
   dispatcher wakes the harness.
4. Advances the schedule via ``mark_run_created`` (bumps ``run_count``,
   sets ``last_session_id``, computes the next ``next_run_at``).

The ticker enqueues run *session* ids, never schedule-row ids — a
schedule id has no ``sessions`` row, so enqueueing it directly makes the
dispatcher fail every tick with ``SessionNotFoundError`` and no loop run
is ever produced.

:func:`recover_stalled_loops` is the periodic safety net for dynamic
loops whose run died without rescheduling itself.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from surogates.channels.constants import session_queue_priority
from surogates.config import INTERRUPT_CHANNEL_PREFIX, enqueue_session
from surogates.scheduled.schedule import (
    DEFAULT_MAX_RUN_SECONDS,
    DYNAMIC_LOOP_STALE_RUN_SECONDS,
    local_iso,
    schedule_from_row,
)
from surogates.session.events import EventType
from surogates.session.execution_context import inherit_context
from surogates.session.provisioning import (
    create_agent_session,
    create_child_session,
)
from surogates.session.store import SessionNotFoundError

logger = logging.getLogger(__name__)


async def materialize_scheduled_run(
    schedule: Any,
    *,
    session_store: Any,
    scheduled_store: Any,
    storage: Any,
    settings: Any,
    redis: Any,
) -> UUID:
    """Create + enqueue one run for a claimed *schedule* row.

    Returns the run session's id.  Idempotent per fire instant: a retry
    of the same ``(schedule, next_run_at)`` converges on the existing run
    via the idempotency key instead of creating a duplicate.
    """
    fire_key = (
        schedule.next_run_at.isoformat() if schedule.next_run_at else "now"
    )
    idempotency_key = f"scheduled:{schedule.id}:{fire_key}"
    is_dynamic_loop = schedule.schedule.get("kind") == "dynamic_loop"

    previous = await _previous_run(session_store, schedule)
    if previous is not None and _still_going(previous, schedule):
        await scheduled_store.mark_run_skipped(
            schedule,
            reason=f"Skipped a run: the previous run ({previous.id}) is still going.",
        )
        return previous.id

    scheduled_config = {
        "scheduled_session_id": str(schedule.id),
        "scheduled_source": schedule.source,
        "scheduled_dynamic_loop": is_dynamic_loop,
        "scheduled_run": run_identity(schedule, previous),
    }

    # When the schedule has a known creator session, every run shares its
    # workspace (so cumulative state across loop iterations persists).
    # Detached schedules — creator deleted or never set — fall back to a
    # fresh per-run workspace and drop parent_id (the sessions.parent_id
    # FK would otherwise reject the insert).
    parent = None
    if schedule.created_from_session_id is not None:
        try:
            parent = await session_store.get_session(
                schedule.created_from_session_id,
            )
        except SessionNotFoundError:
            parent = None

    # What the schedule captured when it was made; rows from before it was
    # captured take the creating chat's current context instead.
    captured = getattr(schedule, "context", None) or None
    try:
        if parent is not None:
            session = await create_child_session(
                store=session_store,
                parent=parent,
                channel="scheduled",
                config=scheduled_config,
                idempotency_key=idempotency_key,
                context=captured,
            )
        else:
            session = await create_agent_session(
                store=session_store,
                storage=storage,
                settings=settings,
                org_id=schedule.org_id,
                user_id=schedule.user_id,
                service_account_id=schedule.service_account_id,
                agent_id=schedule.agent_id,
                channel="scheduled",
                config={**scheduled_config, **inherit_context(None, None, captured)},
                parent_id=None,
                idempotency_key=idempotency_key,
            )
    except IntegrityError:
        # Duplicate fire raced us to the idempotency key; adopt the run
        # the other tick created.  Children inherit org_id from the
        # parent, so look up under whichever org actually owns the row.
        lookup_org = parent.org_id if parent is not None else schedule.org_id
        existing = await session_store.get_session_by_idempotency_key(
            lookup_org,
            idempotency_key,
        )
        if existing is None:
            raise
        session = existing

    await session_store.emit_event(
        session.id,
        EventType.USER_MESSAGE,
        {"content": schedule.prompt, "scheduled_session_id": str(schedule.id)},
    )
    await enqueue_session(
        redis,
        org_id=str(session.org_id),
        agent_id=session.agent_id,
        session_id=session.id,
        priority=session_queue_priority(session),
    )
    await scheduled_store.mark_run_created(schedule, session_id=session.id)
    return session.id


def run_identity(schedule: Any, previous: Any | None) -> dict[str, Any]:
    """What a run is told about the schedule it belongs to."""
    tz = schedule.timezone or "UTC"
    try:
        upcoming = schedule_from_row(schedule).next_after(datetime.now(timezone.utc))
    except ValueError:
        upcoming = None
    return {
        "id": str(schedule.id),
        "name": schedule.name,
        "cadence": schedule.schedule_display,
        "timezone": tz,
        "run_number": (schedule.run_count or 0) + 1,
        "repeat_limit": schedule.repeat_limit,
        "previous_run": None if previous is None else {
            "session_id": str(previous.id),
            "status": previous.status,
            "started_at": local_iso(previous.created_at, tz),
        },
        "last_error": schedule.last_error,
        "next_run_at": None if upcoming is None else local_iso(upcoming, tz),
        "expires_at": local_iso(schedule.expires_at, tz),
    }


async def _previous_run(session_store: Any, schedule: Any) -> Any | None:
    if schedule.last_session_id is None:
        return None
    try:
        return await session_store.get_session(schedule.last_session_id)
    except SessionNotFoundError:
        return None


def _still_going(run: Any, schedule: Any) -> bool:
    """Whether *run* is still at work: active, and not past the schedule's
    run limit, so a wedged run does not hold its schedule forever."""
    if run.status != "active":
        return False
    limit = timedelta(seconds=schedule.max_run_seconds or DEFAULT_MAX_RUN_SECONDS)
    started = run.created_at if run.created_at.tzinfo else run.created_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - started < limit


def ending_notice(schedule: Any) -> str:
    """What the chat that made *schedule* is told when it ended on its own."""
    name = schedule.name or "this schedule"
    runs = schedule.run_count or 0
    ran = f"It ran {runs} time{'' if runs == 1 else 's'}."
    if schedule.ended_reason == "expired":
        return (
            f"The schedule \"{name}\" has ended: it reached its end date "
            f"({local_iso(schedule.expires_at, schedule.timezone or 'UTC')}). {ran} "
            "Make a new one to keep it going."
        )
    if schedule.ended_reason == "limit":
        return (
            f"The schedule \"{name}\" has ended: it ran all {schedule.repeat_limit} "
            "times it was set to run."
        )
    reason = f" The last error: {schedule.last_error.rstrip('.')}." if schedule.last_error else ""
    return (
        f"The schedule \"{name}\" has stopped: its runs kept failing.{reason} {ran} "
        "Fix the cause, then make it again."
    )


async def _announce_ending(session_store: Any, schedule: Any) -> None:
    if schedule.created_from_session_id is None:
        return
    try:
        await session_store.emit_event(
            schedule.created_from_session_id,
            EventType.LOOP_RESULT,
            {
                "run_session_id": str(schedule.last_session_id or ""),
                "scheduled_session_id": str(schedule.id),
                "content": ending_notice(schedule),
                "outcome": "schedule_ended",
                "ended_reason": schedule.ended_reason,
                "run_completed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception:
        logger.warning(
            "Failed to tell session %s that schedule %s ended",
            schedule.created_from_session_id, schedule.id, exc_info=True,
        )


async def recover_stalled_loops(
    *,
    scheduled_store: Any,
    redis: Any,
    session_store: Any | None = None,
    stale_seconds: int = DYNAMIC_LOOP_STALE_RUN_SECONDS,
    limit: int = 100,
) -> None:
    """Sweep dynamic loops whose run stalled, across all tenants, and reap
    schedules that have passed their ``expires_at``.

    Two dynamic-loop failure modes:

    * The worker died mid-run (run row still ``active`` but its lease
      lapsed and it hasn't progressed) — re-enqueue the same run so a
      live worker picks it up.
    * The run reached a terminal state without rescheduling (never called
      ``loop_wait``) — :meth:`recover_stalled_dynamic_loops` reschedules
      it with the fallback delay.

    Plus the expiry sweep: a schedule whose ``expires_at`` falls between its
    last run and its next due instant expires *unclaimed* (the claim query
    excludes expired rows), so it never reaches the completion code and stays
    ``active`` forever. :meth:`expire_active_schedules` transitions those.

    And, given *session_store*, runs open past their time limit are failed
    first, so a stalled loop's run is not requeued past it.
    """
    for schedule in await scheduled_store.plan_unscheduled(limit=limit):
        logger.info("Planned schedule %s: next run %s", schedule.id, schedule.next_run_at)

    # Every writer of a cancel (the runtime's own, the control plane writing
    # the row) is covered here, a tick later, instead of at each of them.
    if session_store is not None:
        for run_id in await scheduled_store.end_runs_of_cancelled(limit=limit):
            if await session_store.pause_if_active(run_id, {"reason": "schedule_cancelled"}):
                logger.info("Stopped run %s of a cancelled schedule", run_id)
            if redis is not None:
                await redis.publish(f"{INTERRUPT_CHANNEL_PREFIX}:{run_id}", "schedule_cancelled")

    if session_store is not None:
        for run_id in await session_store.fail_overdue_scheduled_runs(
            default_seconds=DEFAULT_MAX_RUN_SECONDS, limit=limit,
        ):
            logger.warning("Failed scheduled run %s: past its time limit", run_id)

    expired = await scheduled_store.expire_active_schedules(limit=limit)
    for schedule in expired:
        logger.info(
            "Expired schedule %s (past expires_at) → completed",
            schedule.id,
        )

    # Every way a schedule ends on its own is covered here, the control
    # plane's writes included, a tick later.
    if session_store is not None:
        for schedule in await scheduled_store.claim_unannounced_endings(limit=limit):
            await _announce_ending(session_store, schedule)

    retryable = await scheduled_store.find_retryable_stalled_dynamic_loop_runs(
        stale_seconds=stale_seconds,
        limit=limit,
    )
    for schedule in retryable:
        if schedule.last_session_id is None:
            continue
        await enqueue_session(
            redis,
            org_id=str(schedule.org_id),
            agent_id=schedule.agent_id,
            session_id=schedule.last_session_id,
        )
        logger.warning(
            "Requeued stalled dynamic loop session %s for schedule %s",
            schedule.last_session_id,
            schedule.id,
        )

    recovered = await scheduled_store.recover_stalled_dynamic_loops(
        stale_seconds=stale_seconds,
        limit=limit,
    )
    for schedule in recovered:
        logger.warning(
            "Recovered terminal dynamic loop %s with fallback delay",
            schedule.id,
        )
