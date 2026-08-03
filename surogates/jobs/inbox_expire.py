"""Background job for expiring stale inbox items.

Pending inbox items are actionable only while something is still waiting to
consume the user's response. Two things end that wait: the owning session
going terminal, and — for a question — the blocked tool call giving up. Past
either point the item stays in the history but must stop presenting itself as
actionable, or the user submits an answer no one reads.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import and_, func, or_, select, update

from surogates.db.models import InboxItem, Session
from surogates.session.inbox_payload import ACKNOWLEDGE_ONLY_KINDS
from surogates.tools.builtin.ask_user_question import (
    ASK_USER_QUESTION_MAX_WAIT_SECONDS,
)

logger = logging.getLogger(__name__)

DEFAULT_SWEEP_INTERVAL_SECONDS = 300.0
_TERMINAL_SESSION_STATUSES = frozenset({"completed", "failed", "archived"})

# Added to the tool's own wait before a question is considered dead. The row
# is stamped by the database while the tool counts down on the worker's
# monotonic clock, so the two disagree by whatever skew exists between them.
# Erring long only leaves a dead question listed a minute longer; erring short
# would expire one a live tool could still consume.
_ANSWER_WINDOW_GRACE_SECONDS = 60


async def expire_inbox_items(session_store) -> int:
    """Expire pending inbox items that can no longer be acted on."""
    terminal_sessions = select(Session.id).where(
        Session.status.in_(_TERMINAL_SESSION_STATUSES)
    )
    answer_window = timedelta(
        seconds=ASK_USER_QUESTION_MAX_WAIT_SECONDS + _ANSWER_WINDOW_GRACE_SECONDS
    )
    async with session_store._sf() as db:
        result = await db.execute(
            update(InboxItem)
            .where(
                InboxItem.status == "pending",
                or_(
                    and_(
                        InboxItem.session_id.in_(terminal_sessions),
                        # Acknowledge-only kinds are informational; they
                        # persist until read/acknowledged rather than
                        # expiring on a terminal session.
                        InboxItem.kind.notin_(ACKNOWLEDGE_ONLY_KINDS),
                    ),
                    # A question outlives its tool call on a session that
                    # is still running: the tool returned a timeout to the
                    # LLM and the turn moved on, but nothing else clears
                    # the row. Compared against the database clock, which
                    # is the one that stamped created_at.
                    and_(
                        InboxItem.kind == "input_required",
                        InboxItem.created_at < func.now() - answer_window,
                    ),
                ),
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
        logger.info("Expired %d inbox item(s)", len(ids))
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
