"""Leader-locked ticker that fires due check-in Programs.

Mirrors :class:`surogates.ambient.ticker.AmbientTicker`: acquire the leader
lock, sweep expired check-ins, claim due schedules, materialise each one
(isolating per-row failures), hand the queued openers on, sleep, repeat.

The sweep runs **first**, and that ordering is load-bearing.  Materialisation
skips a patient whose previous check-in is still open, so if expired
invitations were not closed before the roster is evaluated, one unanswered
opener would suppress that patient's check-ins for good.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import sqlalchemy as sa

from surogates.db.models import ProgramInvitationRow

logger = logging.getLogger(__name__)

#: Only a patient we actually reached can fail to reply.  A send that was
#: skipped, cancelled or rejected by the provider is our failure, not theirs.
_REACHED_STATES = ("accepted", "delivered")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def sweep_deadlines(session_factory: Any, *, now: datetime | None = None) -> int:
    """Close check-ins whose deadline has passed unanswered.

    The ``delivery_state`` filter is the point: without it a failed or skipped
    send would be recorded as a patient who stayed silent, which blames the
    patient for our failure to reach them and hides the delivery problem the
    operator actually needs to fix.
    """
    now = now or _utcnow()
    async with session_factory() as db:
        result = await db.execute(
            sa.update(ProgramInvitationRow)
            .where(ProgramInvitationRow.response_state == "awaiting_reply")
            .where(ProgramInvitationRow.delivery_state.in_(_REACHED_STATES))
            .where(ProgramInvitationRow.deadline_at.isnot(None))
            .where(ProgramInvitationRow.deadline_at <= now)
            .values(response_state="no_reply_by_deadline")
        )
        await db.commit()
        return int(result.rowcount or 0)


class ProgramTicker:
    def __init__(
        self,
        store: Any,
        *,
        session_factory: Any,
        materialize: Callable[[Any], Awaitable[None]],
        worker_id: str,
        send_openers: Callable[[], Awaitable[None]] | None = None,
        reconcile: Callable[[], Awaitable[None]] | None = None,
        leader_lock: Any = None,
        tick_interval_seconds: float = 60.0,
        claim_limit: int = 50,
    ) -> None:
        self._store = store
        self._sf = session_factory
        self._materialize = materialize
        self._send_openers = send_openers
        #: Refreshes the mirrored schedules from ops' projection. Supplied by
        #: the caller because fetching it needs an ops base URL and a
        #: runtime-scoped key, neither of which this process configures today.
        self._reconcile = reconcile
        self._worker_id = worker_id
        self._lock = leader_lock
        self._interval = tick_interval_seconds
        self._claim_limit = claim_limit
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def tick_once(self) -> None:
        # Backstop for a missed program_changed publish: a Program paused in
        # ops must stop firing even if the notification never arrived.
        if self._reconcile is not None:
            try:
                await self._reconcile()
            except Exception:
                logger.exception("program ticker failed to reconcile from ops")

        # First, so the roster check below sees expired invitations as closed.
        try:
            await sweep_deadlines(self._sf, now=_utcnow())
        except Exception:
            logger.exception("program ticker failed to sweep deadlines")

        rows = await self._store.claim_due(
            worker_id=self._worker_id, limit=self._claim_limit,
        )
        for row in rows:
            try:
                await self._materialize(row)
            except Exception:
                logger.exception(
                    "program ticker failed to materialize program %s",
                    getattr(row, "program_id", "?"),
                )
                # A failed tick leaves the row past-due holding a lock nobody
                # owns; tell the store or claim_due re-fires it every time the
                # lease lapses, forever.
                try:
                    await self._store.mark_failed(row)
                except Exception:
                    logger.exception(
                        "program ticker could not record the failed tick for "
                        "program %s; it will retry when the lease lapses",
                        getattr(row, "program_id", "?"),
                    )

        # Kept outside the claim so the lease covers database work rather than
        # provider round trips.
        if self._send_openers is not None:
            try:
                await self._send_openers()
            except Exception:
                logger.exception("program ticker failed to send queued openers")

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._lock is None or await self._lock.acquire():
                    try:
                        await self.tick_once()
                    finally:
                        if self._lock is not None:
                            await self._lock.release()
            except Exception:
                logger.exception("program ticker tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
