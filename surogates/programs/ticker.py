"""Leader-locked ticker that fires due check-in Programs.

Mirrors :class:`surogates.ambient.ticker.AmbientTicker`: acquire the leader
lock, sweep expired check-ins, claim due schedules, materialise each one
(isolating per-row failures), hand the queued openers on, sleep, repeat.

The sweep runs **first**, and that ordering is load-bearing.  Materialisation
skips a user whose previous check-in is still open, so if expired
invitations were not closed before the roster is evaluated, one unanswered
opener would suppress that user's check-ins for good.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import sqlalchemy as sa

from surogates.db.models import ProgramInvitationRow

logger = logging.getLogger(__name__)

#: Only a user we actually reached can fail to reply.  A send that was
#: skipped, cancelled or rejected by the provider is our failure, not theirs.
_REACHED_STATES = ("accepted", "delivered")

#: The user answered but the agent never recorded an outcome. Terminal
#: at the deadline, so the next occurrence can reach them again.
_STARTED_STATES = ("replied", "in_progress")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def sweep_deadlines(session_factory: Any, *, now: datetime | None = None) -> int:
    """Close every check-in whose deadline has passed without finishing.

    Two transitions, because two different things go wrong and the operator
    needs to tell them apart:

    * asked and never answered → ``no_reply_by_deadline``.  The
      ``delivery_state`` filter is the point here: without it a failed or
      skipped send would be recorded as a user who stayed silent, blaming
      the user for our failure to reach them and hiding the delivery
      problem that actually needs fixing.
    * answered but never closed → ``incomplete``.  Only ``checkin_outcome``
      moves a row out of ``replied``, so a user who trails off mid-answer,
      a session that errors, or a model that simply never calls the tool would
      leave the row open forever.  That matters beyond the history: an open
      check-in suppresses the user's next one, so without this they drop
      out of the Program silently and permanently.

    Both are terminal, which is what lets the next occurrence reach that
    user again.
    """
    now = now or _utcnow()
    swept = 0
    async with session_factory() as db:
        unanswered = await db.execute(
            sa.update(ProgramInvitationRow)
            .where(ProgramInvitationRow.response_state == "awaiting_reply")
            .where(ProgramInvitationRow.delivery_state.in_(_REACHED_STATES))
            .where(ProgramInvitationRow.deadline_at.isnot(None))
            .where(ProgramInvitationRow.deadline_at <= now)
            .values(response_state="no_reply_by_deadline")
        )
        swept += int(unanswered.rowcount or 0)

        unfinished = await db.execute(
            sa.update(ProgramInvitationRow)
            .where(ProgramInvitationRow.response_state.in_(_STARTED_STATES))
            .where(ProgramInvitationRow.deadline_at.isnot(None))
            .where(ProgramInvitationRow.deadline_at <= now)
            .values(response_state="incomplete")
        )
        swept += int(unfinished.rowcount or 0)

        # Third case: the opener never reached them and the deadline came
        # anyway.  A row still ``queued`` at that point had no dispatcher
        # report at all (the outbox row was lost, or the process died between
        # claiming and handing over): record the delivery failure so the
        # operator sees it, rather than leaving a row that looks in flight.
        unreported = await db.execute(
            sa.update(ProgramInvitationRow)
            .where(ProgramInvitationRow.response_state == "awaiting_reply")
            .where(ProgramInvitationRow.delivery_state == "queued")
            .where(ProgramInvitationRow.deadline_at.isnot(None))
            .where(ProgramInvitationRow.deadline_at <= now)
            .values(
                delivery_state="failed",
                delivery_error="No delivery report by the deadline",
            )
        )
        swept += int(unreported.rowcount or 0)

        # Whatever the delivery failure was, an ``awaiting_reply`` that was
        # never delivered is not an open check-in.  Left as is it suppresses
        # the user's next occurrence forever; ``not_started`` says what
        # happened (they were never asked) and lets the next one reach them.
        never_asked = await db.execute(
            sa.update(ProgramInvitationRow)
            .where(ProgramInvitationRow.response_state == "awaiting_reply")
            .where(ProgramInvitationRow.delivery_state.notin_(_REACHED_STATES))
            .where(ProgramInvitationRow.deadline_at.isnot(None))
            .where(ProgramInvitationRow.deadline_at <= now)
            .values(response_state="not_started")
        )
        swept += int(never_asked.rowcount or 0)

        await db.commit()
        return swept


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
        # The only way a change in ops reaches the runtime: nothing is
        # pushed, so a pause takes effect on the next tick, not before.
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
        # provider round trips. Confirm we still lead before starting them:
        # a backlog of openers is the one phase that can outlive the leader
        # lock, and a second replica running the same pass over the same
        # still-queued rows would message every user twice.
        if self._send_openers is not None:
            if self._lock is not None and hasattr(self._lock, "heartbeat"):
                try:
                    if not await self._lock.heartbeat():
                        logger.warning(
                            "program ticker lost the leader lock before the "
                            "send pass; leaving queued openers for the leader",
                        )
                        return
                except Exception:
                    logger.exception("program ticker heartbeat failed")
                    return
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
