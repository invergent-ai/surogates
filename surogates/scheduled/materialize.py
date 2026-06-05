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
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from surogates.config import enqueue_session
from surogates.scheduled.schedule import DYNAMIC_LOOP_STALE_RUN_SECONDS
from surogates.session.events import EventType
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
    scheduled_config = {
        "scheduled_session_id": str(schedule.id),
        "scheduled_source": schedule.source,
        "scheduled_dynamic_loop": is_dynamic_loop,
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

    try:
        if parent is not None:
            session = await create_child_session(
                store=session_store,
                parent=parent,
                channel="scheduled",
                model=settings.llm.model,
                config=scheduled_config,
                idempotency_key=idempotency_key,
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
                model=settings.llm.model,
                config=scheduled_config,
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
    )
    await scheduled_store.mark_run_created(schedule, session_id=session.id)
    return session.id


async def recover_stalled_loops(
    *,
    scheduled_store: Any,
    redis: Any,
    stale_seconds: int = DYNAMIC_LOOP_STALE_RUN_SECONDS,
    limit: int = 100,
) -> None:
    """Sweep dynamic loops whose run stalled, across all tenants.

    Two failure modes:

    * The worker died mid-run (run row still ``active`` but its lease
      lapsed and it hasn't progressed) — re-enqueue the same run so a
      live worker picks it up.
    * The run reached a terminal state without rescheduling (never called
      ``loop_wait``) — :meth:`recover_stalled_dynamic_loops` reschedules
      it with the fallback delay.
    """
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
