from __future__ import annotations

import asyncio
import logging

from sqlalchemy.exc import IntegrityError

from surogates.config import enqueue_session
from surogates.scheduled.schedule import DYNAMIC_LOOP_STALE_RUN_SECONDS
from surogates.session.events import EventType
from surogates.session.provisioning import create_agent_session, create_child_session
from surogates.session.store import SessionNotFoundError

logger = logging.getLogger(__name__)


class ScheduledSessionRunner:
    def __init__(
        self,
        *,
        settings,
        session_factory,
        session_store,
        scheduled_store,
        redis,
        storage,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._session_store = session_store
        self._scheduled_store = scheduled_store
        self._redis = redis
        self._storage = storage
        self._running = True

    async def run_forever(self) -> None:
        interval = max(5, int(self._settings.scheduled_sessions.tick_interval_seconds))
        while self._running:
            try:
                await self.tick_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduled session tick failed")
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return

    async def shutdown(self) -> None:
        self._running = False

    async def tick_once(self) -> int:
        retryable = await self._scheduled_store.find_retryable_stalled_dynamic_loop_runs(
            agent_id=self._settings.agent_id,
            stale_seconds=DYNAMIC_LOOP_STALE_RUN_SECONDS,
            limit=self._settings.scheduled_sessions.claim_limit,
        )
        for schedule in retryable:
            if schedule.last_session_id is None:
                continue
            await enqueue_session(
                self._redis,
                schedule.agent_id,
                schedule.last_session_id,
            )
            logger.warning(
                "Requeued stalled dynamic loop session %s for schedule %s",
                schedule.last_session_id,
                schedule.id,
            )

        recovered = await self._scheduled_store.recover_stalled_dynamic_loops(
            agent_id=self._settings.agent_id,
            stale_seconds=DYNAMIC_LOOP_STALE_RUN_SECONDS,
            limit=self._settings.scheduled_sessions.claim_limit,
        )
        for schedule in recovered:
            logger.warning(
                "Recovered terminal dynamic loop %s with fallback delay",
                schedule.id,
            )

        claimed = await self._scheduled_store.claim_due(
            agent_id=self._settings.agent_id,
            worker_id=self._settings.worker_id,
            limit=self._settings.scheduled_sessions.claim_limit,
            lease_seconds=self._settings.scheduled_sessions.claim_lease_seconds,
        )
        processed = 0
        for schedule in claimed:
            try:
                await self._run_one(schedule)
            except Exception as exc:
                await self._scheduled_store.mark_run_failed(schedule, error=str(exc))
                logger.exception(
                    "Scheduled session %s failed during run creation",
                    schedule.id,
                )
            else:
                processed += 1
        return processed

    async def _run_one(self, schedule) -> None:
        fire_key = schedule.next_run_at.isoformat() if schedule.next_run_at else "now"
        idempotency_key = f"scheduled:{schedule.id}:{fire_key}"
        is_dynamic_loop = schedule.schedule.get("kind") == "dynamic_loop"
        scheduled_config = {
            "scheduled_session_id": str(schedule.id),
            "scheduled_source": schedule.source,
            "scheduled_dynamic_loop": is_dynamic_loop,
        }

        # When the schedule has a known creator session, every run shares
        # its workspace (so cumulative state across loop iterations
        # persists).  Detached schedules — creator deleted or never set —
        # fall back to a fresh per-run workspace and drop parent_id (the
        # sessions.parent_id FK would otherwise reject the insert).
        parent = None
        if schedule.created_from_session_id is not None:
            try:
                parent = await self._session_store.get_session(
                    schedule.created_from_session_id,
                )
            except SessionNotFoundError:
                parent = None

        try:
            if parent is not None:
                session = await create_child_session(
                    store=self._session_store,
                    parent=parent,
                    channel="scheduled",
                    model=self._settings.llm.model,
                    config=scheduled_config,
                    idempotency_key=idempotency_key,
                )
            else:
                session = await create_agent_session(
                    store=self._session_store,
                    storage=self._storage,
                    settings=self._settings,
                    org_id=schedule.org_id,
                    user_id=schedule.user_id,
                    service_account_id=schedule.service_account_id,
                    agent_id=schedule.agent_id,
                    channel="scheduled",
                    model=self._settings.llm.model,
                    config=scheduled_config,
                    parent_id=None,
                    idempotency_key=idempotency_key,
                )
        except IntegrityError:
            # Children inherit org_id from the parent; look up under
            # whichever org actually owns the row.  In normal operation
            # parent.org_id == schedule.org_id, but we don't enforce
            # that, so use the right scope explicitly.
            lookup_org = parent.org_id if parent is not None else schedule.org_id
            existing = await self._session_store.get_session_by_idempotency_key(
                lookup_org,
                idempotency_key,
            )
            if existing is None:
                raise
            session = existing

        await self._session_store.emit_event(
            session.id,
            EventType.USER_MESSAGE,
            {"content": schedule.prompt, "scheduled_session_id": str(schedule.id)},
        )
        await enqueue_session(self._redis, session.agent_id, session.id)
        await self._scheduled_store.mark_run_created(schedule, session_id=session.id)
