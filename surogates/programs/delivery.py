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
  the message was never delivered.  Dropping that would leave a user we
  never reached at ``awaiting_reply`` until the sweep marked them a
  non-responder — blaming the user for our failure.

This module touches the **delivery** axis only.  Response and escalation are
separate facts and must never move because of a delivery event.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa

from surogates.channels.capabilities import capabilities_for
from surogates.db.models import DeliveryOutbox, Event, ProgramInvitationRow
from surogates.followup.outreach import apply_outreach_status
from surogates.programs.journey import fall_back

logger = logging.getLogger(__name__)

#: Delivery states in the order they may advance.  A status may move a row
#: forward along this line, never back: Meta batches statuses and retries
#: un-acknowledged webhooks, so a stale ``delivered`` can arrive after a
#: ``failed`` and must not resurrect a send that was already known lost.
_ORDER = {"queued": 0, "accepted": 1, "delivered": 2, "failed": 3, "undelivered": 3}

#: Meta status values that move an invitation's delivery axis.  ``sent``
#: adds nothing it does not already know; ``read`` is recorded apart, as
#: ``read_at``, and still reaches outreach, where it says whether a team
#: member opened an alert.
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
            row = await _woken_step_of(db, outbox_id)
        if row is None:
            return False

        if error:
            _advance(row, "failed")
            row.delivery_error = error[:500]
            if row.response_state == "awaiting_reply":
                # Nobody was asked, so nobody is awaited.  Left at
                # ``awaiting_reply`` the row counts as an open check-in and
                # suppresses this user's next occurrence for good.
                row.response_state = "not_started"
            await fall_back(db, row, now=datetime.now(timezone.utc))
        else:
            if provider_message_id:
                row.provider_message_id = provider_message_id
            # A channel that never reports delivery has delivered once the
            # post succeeded; waiting for a receipt would wait forever.
            receipts = capabilities_for(row.platform).receipts
            _advance(row, "accepted" if "delivered" in receipts else "delivered")
        await db.commit()
    return True


async def _woken_step_of(db: Any, outbox_id: int) -> ProgramInvitationRow | None:
    """The step the agent was woken to write, when this outbox row is the
    message of the turn that answered that wake.

    Only that turn's: the first wake of the conversation after the step
    context, with no message of the person's between them, and the row's
    event before any later wake.  A wake held back (a turn under way, a
    question waiting) and then taken with the person's own message, or any
    later turn, is not the step's message, however soon it comes.

    Keyed on the row from then on, so the provider's statuses reach the step
    as they do an opener's.  Served by idx_program_invitations_woken.
    """
    found = (
        await db.execute(
            sa.select(DeliveryOutbox.session_id, DeliveryOutbox.event_id)
            .where(DeliveryOutbox.id == outbox_id)
        )
    ).first()
    if found is None:
        return None
    session_id, said = found
    row = (
        await db.execute(
            sa.select(ProgramInvitationRow)
            .where(ProgramInvitationRow.session_id == session_id)
            .where(ProgramInvitationRow.woken_at.isnot(None))
            .where(ProgramInvitationRow.outbox_id.is_(None))
            .where(ProgramInvitationRow.response_state == "awaiting_reply")
            .order_by(ProgramInvitationRow.woken_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None or not await _answers_the_wake(db, session_id, str(row.id), said):
        return None
    row.outbox_id = outbox_id
    return row


async def _answers_the_wake(db: Any, session_id: Any, invitation_id: str, said: int) -> bool:
    """Whether event *said* belongs to the turn that took the step context."""
    events = (
        await db.execute(
            sa.select(Event.id, Event.type, Event.data)
            .where(Event.session_id == session_id)
            .where(Event.id <= said)
            .where(Event.type.in_(("user.message", "harness.wake")))
            .order_by(Event.id.desc())
            .limit(200)
        )
    ).all()
    # Newest first, back to the step context: exactly one wake (the turn's
    # own), and before it nothing the person wrote.
    wakes = 0
    person_first = False
    for _, kind, data in events:
        data = data or {}
        if kind == "harness.wake":
            wakes += 1
        elif data.get("invitation_id") == invitation_id and data.get("step_event") == "due":
            return wakes == 1 and not person_first
        elif wakes and not data.get("synthetic"):
            person_first = True
    return False


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
    if not provider_message_id or status not in (*_DELIVERY_STATES, "read"):
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

        if status == "read":
            # Read implies delivered; the receipt may well come first.
            moved = _advance(row, "delivered")
            row.read_at = row.read_at or datetime.now(timezone.utc)
        else:
            moved = _advance(row, status)
        if moved and status in ("failed", "undelivered"):
            # Meta often fails a message without a reason; the person was
            # still never asked, so they are not awaited either.
            if reason:
                row.delivery_error = reason[:500]
            if row.response_state == "awaiting_reply":
                row.response_state = "not_started"
            await fall_back(db, row, now=datetime.now(timezone.utc))
        await db.commit()

    if moved:
        logger.info(
            "[programs] delivery %s for invitation carrying %s",
            status, provider_message_id,
        )
    return True


async def _apply_status_anywhere(
    session_factory: Any, *, provider_message_id: str, status: str, reason: str | None,
) -> bool:
    """A provider status for whichever template owns the message id.

    Check-in openers and the templates that reopen a conversation for an
    alert or an operator's message are the two kinds the platform sends
    unprompted; each knows its own rows.
    """
    for apply in (apply_status_callback, apply_outreach_status):
        if await apply(
            session_factory, provider_message_id=provider_message_id,
            status=status, reason=reason,
        ):
            return True
    return False


def schedule_status_apply(
    *, provider_message_id: str, status: str, reason: str | None,
) -> None:
    """Fire-and-forget a status update from synchronous parsing code.

    Deliberately non-blocking and failure-tolerant: a webhook that raises is
    answered non-200 and Meta retries it in a loop.  A lost status costs one
    stale ``delivery_state``; a retry loop costs the channel.
    """
    if _session_factory is None or status not in (*_DELIVERY_STATES, "read"):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # parsed outside an event loop (a unit test, a CLI)

    task = loop.create_task(
        _apply_status_anywhere(
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
