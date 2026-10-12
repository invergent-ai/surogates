"""Get a message to one person without waiting for them to write first.

WhatsApp allows free text only within 24 hours of the person's last message
to that sender.  Inside the window the text is written into their
conversation and delivered like any reply.  Outside it the approved template
goes out and the text is held until the person answers; the inbound pipeline
delivers it then (:func:`deliver_pending`), and the ticker's send pass picks
up anything the inbound path could not.

Two rules keep a conversation intact:

* **Never write into a conversation a worker is running.**  An assistant
  message landing between a tool call and its result breaks the transcript
  the provider is sent on the next turn.  A busy conversation is left for
  the next tick.
* **Every state change is a conditional update only one caller can win**,
  the claim-before-send discipline the check-in openers use: the send pass
  and an inbound reply can reach the same row at the same moment, and the
  person must get each text once.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa

from surogates.db.models import OutreachMessageRow, SessionLease
from surogates.followup import store as followup_store
from surogates.programs.opener import dm_session, whatsapp_session_key
from surogates.session.events import EventType

logger = logging.getLogger(__name__)

__all__ = [
    "SUPPORTED_PLATFORMS",
    "apply_outreach_status",
    "deliver_pending",
    "expire_unanswered",
    "make_outreach_sender",
    "record_outreach_delivery",
    "send_queued_outreach",
    "window_open",
]

SUPPORTED_PLATFORMS = followup_store.OUTREACH_PLATFORMS

#: The provider's customer-service window.
WINDOW = timedelta(hours=24)
#: Margin kept off the end of the window: a text handed to the outbox a few
#: seconds before the window closes can reach the provider after it has.
WINDOW_MARGIN = timedelta(minutes=10)
#: Messages queued this close together share one reopening template — a burst
#: of alerts is one notification.  Anything later sends its own: an urgent
#: alert must not wait behind yesterday's unanswered template.
BATCH_WINDOW = timedelta(minutes=15)
#: How long a template that reopened a conversation waits for an answer
#: before its held texts are given up as undelivered.
UNANSWERED_AFTER = timedelta(hours=72)
#: Rows handled per pass; the rest wait for the next tick.
BATCH = 200

_HELD = ("queued", "awaiting_reply")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def window_open(last_inbound_at: datetime | None, now: datetime) -> bool:
    if last_inbound_at is None:
        return False
    return _as_utc(now) - _as_utc(last_inbound_at) < WINDOW - WINDOW_MARGIN


def _permission(identity: Any) -> str:
    meta = getattr(identity, "platform_meta", None) or {}
    return (meta.get("contact_permission") or {}).get("status") or "none"


async def _busy(session_factory: Any, session_id: uuid.UUID, now: datetime) -> bool:
    """Whether a worker holds this conversation right now."""
    async with session_factory() as db:
        expires = (
            await db.execute(
                sa.select(SessionLease.expires_at)
                .where(SessionLease.session_id == session_id)
            )
        ).scalar_one_or_none()
    # ``session_leases.expires_at`` is naive UTC.
    return expires is not None and _as_utc(expires) > _as_utc(now)


async def _write_text(session_store: Any, session_id: uuid.UUID, row: Any) -> None:
    # An assistant reply as far as the transcript and the outbox are
    # concerned, so it is delivered exactly as the agent's own replies are,
    # and the agent sees what was said when the person answers.
    await session_store.emit_event(
        session_id,
        EventType.LLM_RESPONSE,
        {
            "message": {"role": "assistant", "content": row.text},
            "synthetic": "outreach",
            "outreach_id": str(row.id),
            "outreach_kind": row.kind,
        },
    )


def _claim(row_id: uuid.UUID, *, from_states: tuple[str, ...]):
    return (
        sa.update(OutreachMessageRow)
        .where(OutreachMessageRow.id == row_id)
        .where(OutreachMessageRow.state.in_(from_states))
    )


async def _fail(session_factory: Any, row_id: uuid.UUID, reason: str) -> None:
    async with session_factory() as db:
        await db.execute(
            _claim(row_id, from_states=("queued",)).values(
                state="failed", reason=reason[:500],
            )
        )
        await db.commit()


async def _deliver_now(
    session_factory: Any,
    session_store: Any,
    row: Any,
    *,
    session_id: uuid.UUID,
    now: datetime,
    final_state: str,
) -> bool:
    """Claim *row*, write its text into *session_id*, and give it back on failure."""
    async with session_factory() as db:
        claimed = await db.execute(
            _claim(row.id, from_states=_HELD).values(
                state=final_state, session_id=session_id,
                sent_at=row.sent_at or now,
                delivered_at=now if final_state == "delivered" else None,
            )
        )
        await db.commit()
    if int(claimed.rowcount or 0) != 1:
        return False  # another pass got here first
    try:
        await _write_text(session_store, session_id, row)
    except Exception:
        # Give it back: a held alert must not be lost to one failed write.
        async with session_factory() as db:
            await db.execute(
                _claim(row.id, from_states=(final_state,)).values(
                    state=row.state, session_id=row.session_id,
                    sent_at=row.sent_at, delivered_at=None,
                )
            )
            await db.commit()
        raise
    return True


async def deliver_pending(
    session_factory: Any,
    session_store: Any,
    *,
    org_id: uuid.UUID,
    agent_id: str,
    user_id: uuid.UUID,
    platform: str,
    channel_identifier: str,
    session_id: uuid.UUID,
    now: datetime | None = None,
) -> int:
    """Deliver everything held for this person, now that they have written.

    Called by the inbound pipeline with the session their message landed in.
    When a worker is mid-turn in that session nothing is written: the send
    pass delivers it on its next tick, once the turn is over.
    """
    now = now or _utcnow()
    if await _busy(session_factory, session_id, now):
        return 0
    async with session_factory() as db:
        rows = (
            await db.execute(
                sa.select(OutreachMessageRow)
                .where(OutreachMessageRow.org_id == org_id)
                .where(OutreachMessageRow.agent_id == agent_id)
                .where(OutreachMessageRow.user_id == user_id)
                .where(OutreachMessageRow.platform == platform)
                .where(OutreachMessageRow.channel_identifier == channel_identifier)
                .where(OutreachMessageRow.state.in_(_HELD))
                .order_by(OutreachMessageRow.created_at)
            )
        ).scalars().all()

    delivered = 0
    for row in rows:
        if await _deliver_now(
            session_factory, session_store, row,
            session_id=session_id, now=now, final_state="delivered",
        ):
            delivered += 1
    return delivered


async def expire_unanswered(session_factory: Any, *, now: datetime) -> int:
    """Give up on held texts whose template has gone unanswered too long."""
    async with session_factory() as db:
        result = await db.execute(
            sa.update(OutreachMessageRow)
            .where(OutreachMessageRow.state == "awaiting_reply")
            .where(OutreachMessageRow.sent_at.isnot(None))
            .where(OutreachMessageRow.sent_at <= now - UNANSWERED_AFTER)
            .values(
                state="failed",
                reason=(
                    "The recipient did not answer the template within "
                    f"{int(UNANSWERED_AFTER.total_seconds() // 3600)} hours"
                ),
            )
        )
        await db.commit()
        return int(result.rowcount or 0)


async def send_queued_outreach(
    session_factory: Any,
    *,
    session_store: Any,
    redis: Any,
    delivery_service: Any,
    identity_lookup: Any,
    now: datetime,
    storage: Any = None,
    settings: Any = None,
) -> int:
    """One send pass over held outreach.  Returns how many rows it moved.

    Queued rows are sent; held rows whose recipient has since written (their
    window is open again) are delivered — the inbound path does that too, but
    not while a turn is running.
    """
    await expire_unanswered(session_factory, now=now)

    async with session_factory() as db:
        rows = (
            await db.execute(
                sa.select(OutreachMessageRow)
                .where(OutreachMessageRow.state.in_(_HELD))
                .order_by(OutreachMessageRow.created_at)
                .limit(BATCH)
            )
        ).scalars().all()

    moved = 0
    for row in rows:
        try:
            moved += await _send_one(
                session_factory, row,
                session_store=session_store, redis=redis,
                delivery_service=delivery_service,
                identity_lookup=identity_lookup, now=now,
                storage=storage, settings=settings,
            )
        except Exception:  # noqa: BLE001 — one person, not the queue
            logger.exception(
                "[outreach] could not send message %s; it stays held", row.id,
            )
    return moved


async def _send_one(
    session_factory: Any,
    row: Any,
    *,
    session_store: Any,
    redis: Any,
    delivery_service: Any,
    identity_lookup: Any,
    now: datetime,
    storage: Any,
    settings: Any,
) -> int:
    queued = row.state == "queued"
    if queued and row.platform not in SUPPORTED_PLATFORMS:
        await _fail(session_factory, row.id, f"Outreach is not available on {row.platform}")
        return 1

    identity = await identity_lookup(row.org_id, row.platform, row.user_id)
    if identity is None:
        if queued:
            await _fail(session_factory, row.id, f"No {row.platform} identity for the recipient")
            return 1
        return 0
    if _permission(identity) != "granted":
        if queued:
            await _fail(session_factory, row.id, "No permission to message the recipient")
            return 1
        return 0

    wa_id = identity.platform_user_id
    last = await followup_store.last_inbound(
        session_factory, org_id=row.org_id, platform=row.platform,
        platform_user_id=wa_id, channel_identifier=row.channel_identifier,
    )
    if not window_open(last, now):
        if not queued:
            return 0  # held for an answer that has not come yet
        return await _reopen(
            session_factory, row, session_store=session_store, redis=redis,
            delivery_service=delivery_service, wa_id=wa_id, now=now,
            storage=storage, settings=settings,
        )

    session_id = await dm_session(
        session_store, redis,
        session_factory=session_factory, storage=storage, settings=settings,
        org_id=row.org_id, agent_id=row.agent_id, user_id=row.user_id,
        platform=row.platform, session_key=whatsapp_session_key(wa_id),
        channel_id=wa_id, sender=row.channel_identifier,
    )
    if await _busy(session_factory, session_id, now):
        return 0  # a turn is running in that conversation; next tick
    delivered = await _deliver_now(
        session_factory, session_store, row, session_id=session_id, now=now,
        # A held text arriving because they answered is delivered; a queued
        # one written straight into an open window has only been sent.
        final_state="sent" if queued else "delivered",
    )
    return int(delivered)


async def _reopen(
    session_factory: Any,
    row: Any,
    *,
    session_store: Any,
    redis: Any,
    delivery_service: Any,
    wa_id: str,
    now: datetime,
    storage: Any,
    settings: Any,
) -> int:
    """The window is closed: hold the text and send the reopening template."""
    if not row.template_name or not row.template_language:
        await _fail(
            session_factory, row.id,
            "The recipient's 24-hour window is closed and no approved "
            "template is configured to reopen it",
        )
        return 1

    session_id = await dm_session(
        session_store, redis,
        session_factory=session_factory, storage=storage, settings=settings,
        org_id=row.org_id, agent_id=row.agent_id, user_id=row.user_id,
        platform=row.platform, session_key=whatsapp_session_key(wa_id),
        channel_id=wa_id, sender=row.channel_identifier,
    )
    async with session_factory() as db:
        batched = (
            await db.execute(
                sa.select(sa.func.count())
                .select_from(OutreachMessageRow)
                .where(OutreachMessageRow.org_id == row.org_id)
                .where(OutreachMessageRow.agent_id == row.agent_id)
                .where(OutreachMessageRow.user_id == row.user_id)
                .where(OutreachMessageRow.platform == row.platform)
                .where(OutreachMessageRow.channel_identifier == row.channel_identifier)
                .where(OutreachMessageRow.state == "awaiting_reply")
                .where(OutreachMessageRow.outbox_id.isnot(None))
                .where(OutreachMessageRow.sent_at > now - BATCH_WINDOW)
            )
        ).scalar_one() > 0
        claimed = await db.execute(
            _claim(row.id, from_states=("queued",)).values(
                state="awaiting_reply", session_id=session_id, sent_at=now,
            )
        )
        await db.commit()
    if int(claimed.rowcount or 0) != 1:
        return 0
    if batched:
        return 1  # a template went out moments ago; this text rides on it

    try:
        event_id = await session_store.emit_synthetic_user_message(
            session_id,
            content=f"[Outreach opener sent: template '{row.template_name}']",
            synthetic="outreach_opener",
            metadata={"outreach_id": str(row.id), "template_name": row.template_name},
        )
        outbox_id = await delivery_service.enqueue(
            session_id, event_id, row.platform,
            {
                "wa_id": wa_id,
                "phone_number_id": row.channel_identifier,
                "channel_identifier": row.channel_identifier,
            },
            {"template": {"name": row.template_name, "language": row.template_language}},
        )
    except Exception:
        async with session_factory() as db:
            await db.execute(
                _claim(row.id, from_states=("awaiting_reply",)).values(
                    state="queued", session_id=None, sent_at=None,
                )
            )
            await db.commit()
        raise
    # The outbox row is how the dispatcher's verdict finds this template.
    async with session_factory() as db:
        await db.execute(
            sa.update(OutreachMessageRow)
            .where(OutreachMessageRow.id == row.id)
            .values(outbox_id=int(outbox_id) if outbox_id is not None else None)
        )
        await db.commit()
    return 1


# ---------------------------------------------------------------------------
# Delivery results for reopening templates
# ---------------------------------------------------------------------------


async def _fail_reopened(db: Any, opener: Any, reason: str) -> int:
    """Fail a template's row and every text that rode on it."""
    result = await db.execute(
        sa.update(OutreachMessageRow)
        .where(OutreachMessageRow.org_id == opener.org_id)
        .where(OutreachMessageRow.agent_id == opener.agent_id)
        .where(OutreachMessageRow.user_id == opener.user_id)
        .where(OutreachMessageRow.channel_identifier == opener.channel_identifier)
        .where(OutreachMessageRow.state == "awaiting_reply")
        .where(OutreachMessageRow.sent_at >= opener.sent_at)
        .where(OutreachMessageRow.sent_at < opener.sent_at + BATCH_WINDOW)
        .values(state="failed", reason=reason[:500])
    )
    return int(result.rowcount or 0)


async def record_outreach_delivery(
    session_factory: Any,
    outbox_id: int,
    *,
    provider_message_id: str | None,
    error: str | None,
) -> bool:
    """The dispatcher's verdict on a reopening template.  True when it was one."""
    async with session_factory() as db:
        opener = (
            await db.execute(
                sa.select(OutreachMessageRow)
                .where(OutreachMessageRow.outbox_id == outbox_id)
            )
        ).scalar_one_or_none()
        if opener is None:
            return False
        if error:
            # A template that never left is not a recipient who stayed
            # silent: say so, now, instead of 72 hours from now.
            await _fail_reopened(db, opener, f"The reopening template was not sent: {error}")
        elif provider_message_id:
            opener.provider_message_id = provider_message_id
        await db.commit()
    return True


async def apply_outreach_status(
    session_factory: Any,
    *,
    provider_message_id: str,
    status: str,
    reason: str | None,
) -> bool:
    """A provider status for a reopening template.  True when it was one."""
    async with session_factory() as db:
        opener = (
            await db.execute(
                sa.select(OutreachMessageRow)
                .where(OutreachMessageRow.provider_message_id == provider_message_id)
            )
        ).scalar_one_or_none()
        if opener is None:
            return False
        if status in ("failed", "undelivered"):
            await _fail_reopened(
                db, opener,
                f"The reopening template was not delivered: {reason or status}",
            )
            await db.commit()
    return True


def make_outreach_sender(
    *,
    session_factory: Any,
    session_store: Any,
    redis: Any,
    delivery_service: Any,
    identity_lookup: Any,
    storage: Any = None,
    settings: Any = None,
):
    """The ticker's ``async (now) -> int`` outreach pass, wired for production."""

    async def _send(now: datetime) -> int:
        return await send_queued_outreach(
            session_factory,
            session_store=session_store, redis=redis,
            delivery_service=delivery_service, identity_lookup=identity_lookup,
            now=now, storage=storage, settings=settings,
        )

    return _send
