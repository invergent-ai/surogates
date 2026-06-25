"""Background job for expiring stale inbox items.

Pending inbox items are actionable only while their owning session can still
consume a user response. Once the session is terminal, the item stays in the
history but should no longer appear as actionable.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import func, select, update

from surogates.db.models import InboxItem, Session
from surogates.session.inbox_payload import ACKNOWLEDGE_ONLY_KINDS

logger = logging.getLogger(__name__)

DEFAULT_SWEEP_INTERVAL_SECONDS = 300.0
_TERMINAL_SESSION_STATUSES = frozenset({"completed", "failed", "archived"})


async def expire_inbox_items(session_store) -> int:
    """Expire pending inbox items whose sessions are terminal."""
    terminal_sessions = select(Session.id).where(
        Session.status.in_(_TERMINAL_SESSION_STATUSES)
    )
    async with session_store._sf() as db:
        result = await db.execute(
            update(InboxItem)
            .where(
                InboxItem.status == "pending",
                InboxItem.session_id.in_(terminal_sessions),
                # Acknowledge-only kinds are informational; they persist until
                # read/acknowledged rather than expiring on a terminal session.
                InboxItem.kind.notin_(ACKNOWLEDGE_ONLY_KINDS),
            )
            .values(
                status="expired",
                updated_at=func.now(),
            )
            .returning(InboxItem.id)
        )
        ids = list(result.scalars().all())
        await db.commit()

    if ids:
        logger.info("Expired %d inbox item(s) for terminal sessions", len(ids))
    return len(ids)


async def run_expire_loop(
    session_store,
    *,
    interval_seconds: float = DEFAULT_SWEEP_INTERVAL_SECONDS,
) -> None:
    """Run the inbox-expire sweeper until cancelled."""
    while True:
        try:
            await expire_inbox_items(session_store)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Inbox expire sweep failed")
        await asyncio.sleep(interval_seconds)
