"""Apply WhatsApp delivery-status callbacks to check-in invitations.

Meta reports send failures asynchronously: the ``POST /messages`` call returns
200 with a message id, and only a later status webhook says the message was
never delivered.  The channel parser logs those and drops them, so without this
a patient we never reached would sit at ``awaiting_reply`` until the deadline
sweep marked them a non-responder — blaming the patient for our failure to
reach them.

This touches the **delivery** axis only.  Response and escalation are separate
facts and must never move because of a delivery status.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import sqlalchemy as sa

from surogates.db.models import ProgramInvitationRow

logger = logging.getLogger(__name__)

#: Meta status values worth recording.  Anything else (``read``, ``sent``)
#: tells us nothing the invitation does not already know.
_DELIVERY_STATES = {"delivered", "failed", "undelivered"}

#: Set once at worker startup by :func:`register_status_applier`.  The channel
#: parser is synchronous and holds no database handle, so it cannot apply a
#: status itself; this is the seam that lets it hand one over.
_session_factory: Any | None = None


def register_status_applier(session_factory: Any) -> None:
    """Give the channel parser a database handle for delivery statuses."""
    global _session_factory
    _session_factory = session_factory


def schedule_status_apply(
    *, provider_message_id: str, status: str, reason: str | None,
) -> None:
    """Fire-and-forget a status update from synchronous parsing code.

    Deliberately non-blocking and failure-tolerant: a status webhook that
    raises would be answered non-200, and Meta would retry it in a loop.  A
    lost status costs one stale delivery_state; a retry loop costs the channel.
    """
    if _session_factory is None or status not in _DELIVERY_STATES:
        return
    try:
        asyncio.get_running_loop().create_task(
            apply_status_callback(
                _session_factory,
                provider_message_id=provider_message_id,
                status=status,
                reason=reason,
            )
        )
    except RuntimeError:
        # Parsed outside an event loop (a unit test, a CLI). Nothing to do.
        pass


async def apply_status_callback(
    session_factory: Any,
    *,
    provider_message_id: str,
    status: str,
    reason: str | None,
) -> bool:
    """Record *status* against the invitation carrying *provider_message_id*.

    Returns ``True`` when an invitation matched.  Most statuses belong to
    ordinary replies with no invitation behind them, so ``False`` is the common
    case and is not an error.
    """
    if not provider_message_id:
        return False

    async with session_factory() as db:
        row = (
            await db.execute(
                sa.select(ProgramInvitationRow)
                .where(
                    ProgramInvitationRow.provider_message_id
                    == provider_message_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return False

        row.delivery_state = status
        if reason:
            row.reason = reason[:500]
        await db.commit()

    logger.info(
        "[programs] delivery %s for invitation carrying %s",
        status, provider_message_id,
    )
    return True
