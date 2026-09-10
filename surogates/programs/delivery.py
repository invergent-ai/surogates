"""Keep an invitation's delivery axis in step with what actually happened.

Two things feed it, both from the **channels** process — the one that runs the
outbox dispatcher and receives Meta's webhooks:

* the dispatcher's own result, via :func:`record_outbox_result`, which is how
  the invitation learns the provider's message id in the first place.  That
  id does not exist until the dispatcher has posted, minutes after enqueue,
  so the invitation is keyed on the outbox row and the dispatcher reports
  back through it;
* Meta's asynchronous status webhooks, via :func:`apply_status_callback`.
  ``POST /messages`` returns 200 with an id and only a later webhook says
  the message was never delivered.  Dropping that would leave a patient we
  never reached at ``awaiting_reply`` until the sweep marked them a
  non-responder — blaming the patient for our failure.

This module touches the **delivery** axis only.  Response and escalation are
separate facts and must never move because of a delivery event.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import sqlalchemy as sa

from surogates.db.models import ProgramInvitationRow

logger = logging.getLogger(__name__)

#: Delivery states in the order they may advance.  A status may move a row
#: forward along this line, never back: Meta batches statuses and retries
#: un-acknowledged webhooks, so a stale ``delivered`` can arrive after a
#: ``failed`` and must not resurrect a send that was already known lost.
_ORDER = {"queued": 0, "accepted": 1, "delivered": 2, "failed": 3, "undelivered": 3}

#: Meta status values worth recording.  ``sent`` and ``read`` add nothing the
#: invitation does not already know.
_DELIVERY_STATES = frozenset({"delivered", "failed", "undelivered"})

#: Set once at channels-process startup.  The webhook parser is synchronous
#: and holds no database handle, so it hands statuses over through this.
_session_factory: Any | None = None

#: Strong references to in-flight status tasks.  asyncio keeps only a weak
#: reference to a running task, so without this a status write could be
#: garbage-collected mid-flight.
_inflight: set[asyncio.Task] = set()


def register_status_applier(session_factory: Any) -> None:
    """Give the webhook parser a database handle for delivery statuses.

    Must be called in the process that receives the webhooks.  Registering
    it anywhere else satisfies the check and silently discards every status.
    """
    global _session_factory
    _session_factory = session_factory


def _advance(row: ProgramInvitationRow, status: str) -> bool:
    """Move the delivery axis forward to *status*; refuse to move it back."""
    if _ORDER.get(status, -1) < _ORDER.get(row.delivery_state, -1):
        return False
    row.delivery_state = status
    return True


async def record_outbox_result(
    session_factory: Any,
    outbox_id: int,
    *,
    provider_message_id: str | None,
    error: str | None,
) -> bool:
    """The dispatcher's verdict on the outbox row carrying an opener.

    On success the invitation learns its provider id — the only key a later
    status webhook can match on — and advances to ``accepted``.  On a
    permanent failure it advances to ``failed`` with the provider's wording.

    Returns ``True`` when an invitation was keyed on *outbox_id*.  Almost every
    outbox row is an ordinary reply with no invitation behind it, so ``False``
    is the common case and not an error.
    """
    async with session_factory() as db:
        row = (
            await db.execute(
                sa.select(ProgramInvitationRow)
                .where(ProgramInvitationRow.outbox_id == outbox_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return False

        if error:
            _advance(row, "failed")
            row.delivery_error = error[:500]
            if row.response_state == "awaiting_reply":
                # Nobody was asked, so nobody is awaited.  Left at
                # ``awaiting_reply`` the row counts as an open check-in and
                # suppresses this patient's next occurrence for good.
                row.response_state = "not_started"
        else:
            if provider_message_id:
                row.provider_message_id = provider_message_id
            _advance(row, "accepted")
        await db.commit()
    return True


async def apply_status_callback(
    session_factory: Any,
    *,
    provider_message_id: str,
    status: str,
    reason: str | None,
) -> bool:
    """Record a Meta status against the invitation carrying *provider_message_id*.

    Returns ``True`` when an invitation matched.  Most statuses belong to
    ordinary replies with no invitation behind them.
    """
    if not provider_message_id or status not in _DELIVERY_STATES:
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

        moved = _advance(row, status)
        if moved and reason and status in ("failed", "undelivered"):
            row.delivery_error = reason[:500]
        await db.commit()

    if moved:
        logger.info(
            "[programs] delivery %s for invitation carrying %s",
            status, provider_message_id,
        )
    return True


def schedule_status_apply(
    *, provider_message_id: str, status: str, reason: str | None,
) -> None:
    """Fire-and-forget a status update from synchronous parsing code.

    Deliberately non-blocking and failure-tolerant: a webhook that raises is
    answered non-200 and Meta retries it in a loop.  A lost status costs one
    stale ``delivery_state``; a retry loop costs the channel.
    """
    if _session_factory is None or status not in _DELIVERY_STATES:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # parsed outside an event loop (a unit test, a CLI)

    task = loop.create_task(
        apply_status_callback(
            _session_factory,
            provider_message_id=provider_message_id,
            status=status,
            reason=reason,
        )
    )
    _inflight.add(task)

    def _done(t: asyncio.Task) -> None:
        _inflight.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.warning(
                "[programs] status %s for %s was not recorded",
                status, provider_message_id, exc_info=t.exception(),
            )

    task.add_done_callback(_done)
