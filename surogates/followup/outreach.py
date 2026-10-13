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
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.orm import aliased

from surogates.channels.identity import readable_phone
from surogates.db.models import (
    ChannelContactRow,
    ChannelIdentity,
    DeliveryOutbox,
    ProgramInvitationRow,
    Event,
    FollowupSettingsRow,
    OutreachMessageRow,
    SessionLease,
    User,
)
from surogates.followup import store as followup_store
from surogates.followup.eligibility import as_utc
from surogates.programs.opener import dm_session, whatsapp_session_key
from surogates.session.events import OUTREACH_OPENER_MARKER, EventType

logger = logging.getLogger(__name__)

__all__ = [
    "apply_outreach_status",
    "deliver_pending",
    "record_outreach_delivery",
    "send_queued_outreach",
    "window_open",
]

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
#: The same for a team member's message to a person, unless the agent's
#: settings say otherwise: whoever asked should hear about it the same day.
OPERATOR_UNANSWERED_HOURS = 24
#: How far back the outcome pass looks.  Longer than any wait above, so
#: every held text has settled inside it.
OUTCOME_LOOKBACK = timedelta(days=4)
#: The team's notices about a message's outcome, overridable per agent in
#: ``followup_settings.texts`` like every other platform wording.
#: Minutes a red alert may wait unopened before the team is reminded,
#: unless the agent's settings say otherwise.  Never below the burst
#: window: a reminder sooner would ride on the alert's own template.
RED_ALERT_REMINDER_MINUTES = 30
DEFAULT_TEXTS = {
    "delivered_notice": "✅ {name} received your message ({time}).",
    "undelivered_notice": "⚠️ {name} did not receive your message: {why}. Call them? {phone}",
    "red_alert_reminder": "🔴 Reminder, not yet opened after {minutes} minutes:\n\n{text}",
    "person_failure_notice": "⚠️ A message to {name} did not arrive: {why}. Call them? {phone}",
}
#: Rows handled per pass; the rest wait for the next tick.
BATCH = 200

_HELD = ("queued", "awaiting_reply")


def window_open(last_inbound_at: datetime | None, now: datetime) -> bool:
    if last_inbound_at is None:
        return False
    return as_utc(now) - as_utc(last_inbound_at) < WINDOW - WINDOW_MARGIN


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
    return expires is not None and as_utc(expires) > as_utc(now)


async def _transition(
    session_factory: Any, row_id: uuid.UUID, *, from_states: tuple[str, ...], **values: Any,
) -> bool:
    """Move a row out of *from_states*; True only for the caller that won."""
    async with session_factory() as db:
        result = await db.execute(
            sa.update(OutreachMessageRow)
            .where(OutreachMessageRow.id == row_id)
            .where(OutreachMessageRow.state.in_(from_states))
            .values(**values)
        )
        await db.commit()
    return int(result.rowcount or 0) == 1


async def _fail(session_factory: Any, row_id: uuid.UUID, reason: str) -> int:
    await _transition(
        session_factory, row_id, from_states=("queued",),
        state="failed", reason=reason[:500],
    )
    return 1


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
    if not await _transition(
        session_factory, row.id, from_states=_HELD,
        state=final_state, session_id=session_id, sent_at=row.sent_at or now,
        delivered_at=now if final_state == "delivered" else None,
    ):
        return False  # another pass got here first
    try:
        await _write_text(session_store, session_id, row)
    except Exception:
        # Give it back: a held alert must not be lost to one failed write.
        await _transition(
            session_factory, row.id, from_states=(final_state,),
            state=row.state, session_id=row.session_id, sent_at=row.sent_at,
            delivered_at=None,
        )
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

    Called by the inbound pipeline on every message, so the common case —
    nothing held — costs one query.  When a worker is mid-turn in that
    session nothing is written: the send pass delivers it once the turn is
    over.
    """
    now = now or datetime.now(timezone.utc)
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
    if not rows or await _busy(session_factory, session_id, now):
        return 0

    delivered = 0
    for row in rows:
        if await _deliver_now(
            session_factory, session_store, row,
            session_id=session_id, now=now, final_state="delivered",
        ):
            delivered += 1
    return delivered


def operator_unanswered_hours(settings: Any) -> int:
    return int(getattr(settings, "operator_unanswered_hours", None) or OPERATOR_UNANSWERED_HOURS)


def _unanswered(hours: int) -> str:
    return f"The recipient did not answer the template within {hours} hours"


async def _expire_unanswered(session_factory: Any, *, now: datetime) -> int:
    """Give up on held texts whose template has gone unanswered too long."""
    held = (
        sa.update(OutreachMessageRow)
        .where(OutreachMessageRow.state == "awaiting_reply")
        .where(OutreachMessageRow.sent_at.isnot(None))
    )
    async with session_factory() as db:
        result = await db.execute(
            held.where(OutreachMessageRow.kind != "operator")
            .where(OutreachMessageRow.sent_at <= now - UNANSWERED_AFTER)
            .values(state="failed", reason=_unanswered(int(UNANSWERED_AFTER.total_seconds() // 3600)))
        )
        expired = int(result.rowcount or 0)
        operator = (
            await db.execute(
                sa.select(OutreachMessageRow.id, OutreachMessageRow.sent_at, FollowupSettingsRow)
                .outerjoin(FollowupSettingsRow, sa.and_(
                    FollowupSettingsRow.org_id == OutreachMessageRow.org_id,
                    FollowupSettingsRow.agent_id == OutreachMessageRow.agent_id,
                ))
                .where(OutreachMessageRow.state == "awaiting_reply")
                .where(OutreachMessageRow.kind == "operator")
                .where(OutreachMessageRow.sent_at.isnot(None))
                .where(OutreachMessageRow.sent_at <= now - timedelta(hours=1))
            )
        ).all()
        for row_id, sent_at, settings in operator:
            hours = operator_unanswered_hours(settings)
            if as_utc(sent_at) <= as_utc(now) - timedelta(hours=hours):
                result = await db.execute(
                    held.where(OutreachMessageRow.id == row_id)
                    .values(state="failed", reason=_unanswered(hours))
                )
                expired += int(result.rowcount or 0)
        await db.commit()
        return expired


def _requester(created_by: str | None) -> uuid.UUID | None:
    """The team member who asked for a message, from ``team:<uid>``."""
    prefix, _, raw = (created_by or "").partition(":")
    if prefix not in ("team", "care-team"):
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


async def describe_person(
    session_factory: Any, *, org_id: uuid.UUID, agent_id: str, user_id: uuid.UUID,
) -> tuple[str, str, str]:
    """(name, readable phone, timezone) of a person, as the team knows them."""
    rows = await followup_store.enrollments(
        session_factory, org_id=org_id, agent_id=agent_id, user_id=user_id,
    )
    enrollment = followup_store.labelling_enrollment(rows)
    async with session_factory() as db:
        user = await db.get(User, user_id)
        wa_id = (
            await db.execute(
                sa.select(ChannelIdentity.platform_user_id)
                .where(ChannelIdentity.org_id == org_id)
                .where(ChannelIdentity.platform == "whatsapp")
                .where(ChannelIdentity.user_id == user_id)
            )
        ).scalars().first()
    zones = await followup_store.protocol_timezones(
        session_factory, org_id=org_id, agent_id=agent_id,
    )
    zone = zones.get(getattr(enrollment, "protocol", None)) or next(iter(zones.values()), "UTC")
    name = followup_store.person_label(user.display_name if user else None, enrollment)
    return name or "?", readable_phone(wa_id) if wa_id else "?", zone


def plain_reason(reason: str | None) -> str:
    """Why a message did not arrive, in words a team member can act on."""
    text = (reason or "").strip()
    lowered = text.lower()
    if "did not answer the template" in lowered:
        return f"no answer to the opening template ({text.rsplit('within ', 1)[-1]})"
    if "131047" in lowered:
        return "their 24h WhatsApp window had closed"
    if "132015" in lowered or "paused" in lowered:
        return "Meta has paused the WhatsApp template"
    if "132016" in lowered or "disabled" in lowered or "rejected" in lowered:
        return "Meta has disabled or rejected the WhatsApp template"
    if "permission" in lowered:
        return "no permission to message them"
    if "no whatsapp identity" in lowered:
        return "no WhatsApp number on record"
    if "no approved template" in lowered or "no approved" in lowered:
        return "their 24h window is closed and no approved template is chosen"
    if "template was not" in lowered:
        return f"WhatsApp refused the template ({text.split(': ', 1)[-1][:160]})"
    return text[:200] or "unknown reason"


async def _report_outcomes(session_factory: Any, *, now: datetime) -> int:
    """Tell a team member what became of a message they asked for.

    One notice per message, keyed on it: a failure of anything they asked
    for (their message, the welcome of someone they enrolled, a notice to a
    colleague), and the delivery of their own message after a held wait.  A
    text written straight into an open window needs none: the tool already
    told them it went out.
    """
    notice = aliased(OutreachMessageRow)
    told = sa.exists().where(
        notice.org_id == OutreachMessageRow.org_id,
        notice.dedupe_key.like(
            "outcome:" + sa.cast(OutreachMessageRow.id, sa.Text) + ":%"
        ),
    )
    async with session_factory() as db:
        rows = (
            await db.execute(
                sa.select(OutreachMessageRow)
                .where(sa.or_(
                    OutreachMessageRow.state == "failed",
                    sa.and_(
                        OutreachMessageRow.kind == "operator",
                        OutreachMessageRow.state == "delivered",
                    ),
                ))
                .where(OutreachMessageRow.created_at >= now - OUTCOME_LOOKBACK)
                .where(sa.or_(
                    OutreachMessageRow.created_by.like("team:%"),
                    OutreachMessageRow.created_by.like("care-team:%"),
                ))
                .where(~told)
                .limit(BATCH)
            )
        ).scalars().all()
    reported = 0
    for row in rows:
        requester = _requester(row.created_by)
        if requester is None:
            continue
        try:
            settings = await followup_store.get_settings(
                session_factory, org_id=row.org_id, agent_id=row.agent_id,
            )
            name, phone, zone = await describe_person(
                session_factory, org_id=row.org_id, agent_id=row.agent_id, user_id=row.user_id,
            )
            texts = (settings.texts or {}) if settings else {}
            if row.state == "delivered":
                at = as_utc(row.delivered_at or now).astimezone(ZoneInfo(zone))
                text = followup_store.render_text(
                    texts.get("delivered_notice"), DEFAULT_TEXTS["delivered_notice"],
                    name=name, time=f"{at:%H:%M}", phone=phone,
                )
            else:
                text = followup_store.render_text(
                    texts.get("undelivered_notice"), DEFAULT_TEXTS["undelivered_notice"],
                    name=name, why=plain_reason(row.reason), phone=phone,
                )
            queued, _ = await followup_store.notify_team(
                session_factory, settings, org_id=row.org_id, agent_id=row.agent_id,
                kind="notice", text=text, created_by="outreach",
                subject_user_id=row.user_id, dedupe_prefix=f"outcome:{row.id}",
                only_user_id=requester, fallback_sender=row.channel_identifier,
            )
            reported += queued
        except Exception:  # noqa: BLE001 — one notice, not the pass
            logger.exception("[outreach] could not report the outcome of %s", row.id)
    return reported


def red_alert_reminder_minutes(settings: Any) -> int:
    minutes = getattr(settings, "red_alert_reminder_minutes", None) or RED_ALERT_REMINDER_MINUTES
    return max(int(minutes), int(BATCH_WINDOW.total_seconds() // 60))


async def _remind_unseen_red_alerts(session_factory: Any, *, now: datetime) -> int:
    """Send a red alert again, once, when a team member has not opened it.

    "Not opened" is a held alert: its template went out and its recipient
    has not answered.  The reminder goes to the whole team, so a colleague
    can act when the first recipient is away from their phone.  Claiming
    the escalation's rows first makes it one reminder whichever pass gets
    there, and never a second.
    """
    async with session_factory() as db:
        rows = (
            await db.execute(
                sa.select(OutreachMessageRow, FollowupSettingsRow)
                .outerjoin(FollowupSettingsRow, sa.and_(
                    FollowupSettingsRow.org_id == OutreachMessageRow.org_id,
                    FollowupSettingsRow.agent_id == OutreachMessageRow.agent_id,
                ))
                .where(OutreachMessageRow.kind == "alert")
                .where(OutreachMessageRow.severity == "red")
                .where(OutreachMessageRow.reminded_at.is_(None))
                .where(OutreachMessageRow.dedupe_key.like("escalation:%"))
                .where(OutreachMessageRow.state.in_(_HELD))
                .where(OutreachMessageRow.created_at <= now - BATCH_WINDOW)
                .where(OutreachMessageRow.created_at >= now - timedelta(days=1))
                .limit(BATCH)
            )
        ).all()
    due: dict[str, tuple[Any, Any]] = {}
    for row, settings in rows:
        wait = timedelta(minutes=red_alert_reminder_minutes(settings))
        if as_utc(row.created_at) <= as_utc(now) - wait:
            due.setdefault(row.dedupe_key.rsplit(":", 1)[0], (row, settings))

    reminded = 0
    for escalation, (row, settings) in due.items():
        async with session_factory() as db:
            claimed = await db.execute(
                sa.update(OutreachMessageRow)
                .where(OutreachMessageRow.org_id == row.org_id)
                .where(OutreachMessageRow.dedupe_key.like(f"{escalation}:%"))
                .where(OutreachMessageRow.reminded_at.is_(None))
                .values(reminded_at=now)
            )
            await db.commit()
        if not int(claimed.rowcount or 0):
            continue
        minutes = red_alert_reminder_minutes(settings)
        queued, why_not = await followup_store.notify_team(
            session_factory, settings, org_id=row.org_id, agent_id=row.agent_id,
            kind="alert", severity="red", created_by="reminder",
            text=followup_store.render_text(
                ((settings.texts or {}) if settings else {}).get("red_alert_reminder"),
                DEFAULT_TEXTS["red_alert_reminder"], minutes=minutes, text=row.text,
            ),
            subject_user_id=row.subject_user_id,
            dedupe_prefix="reminder:" + escalation.removeprefix("escalation:"),
            fallback_sender=row.channel_identifier,
        )
        reminded += queued
        invitation_id = escalation.split(":")[1]
        if invitation_id != "-":
            async with session_factory() as db:
                await db.execute(
                    sa.update(ProgramInvitationRow)
                    .where(ProgramInvitationRow.id == uuid.UUID(invitation_id))
                    .values(reminder_state="reminded" if queued else "reminder_failed")
                )
                await db.commit()
        if not queued:
            logger.warning("[outreach] red alert reminder for %s reached nobody: %s", escalation, why_not)
    return reminded


async def _surface_person_failures(session_factory: Any, *, now: datetime) -> int:
    """Tell the team when the agent or a Program could not reach a person.

    A check-in opener or a welcome that failed would otherwise only show in
    a run history nobody reads.  One notice per person per day of failure,
    whatever number of messages failed that day.  Whatever a team member
    asked for is reported to them by :func:`_report_outcomes` instead.
    """
    since = now - OUTCOME_LOOKBACK
    async with session_factory() as db:
        invitations = (
            await db.execute(
                sa.select(
                    ProgramInvitationRow.org_id, ProgramInvitationRow.agent_id,
                    ProgramInvitationRow.user_id, ProgramInvitationRow.created_at,
                    sa.func.coalesce(ProgramInvitationRow.delivery_error, ProgramInvitationRow.reason),
                )
                .where(sa.or_(
                    ProgramInvitationRow.delivery_state.in_(("failed", "undelivered")),
                    sa.and_(
                        ProgramInvitationRow.delivery_state == "canceled",
                        ProgramInvitationRow.reason.ilike("%permission%"),
                    ),
                ))
                .where(ProgramInvitationRow.created_at >= since)
                .limit(BATCH)
            )
        ).all()
        messages = (
            await db.execute(
                sa.select(
                    OutreachMessageRow.org_id, OutreachMessageRow.agent_id,
                    OutreachMessageRow.user_id, OutreachMessageRow.created_at,
                    OutreachMessageRow.reason,
                )
                .where(OutreachMessageRow.state == "failed")
                .where(OutreachMessageRow.created_at >= since)
                .where(sa.or_(
                    OutreachMessageRow.created_by.is_(None),
                    sa.not_(sa.or_(
                        OutreachMessageRow.created_by.like("team:%"),
                        OutreachMessageRow.created_by.like("care-team:%"),
                    )),
                ))
                .limit(BATCH)
            )
        ).all()
    failures: dict[str, tuple] = {}
    for org_id, agent_id, user_id, at, reason in sorted(
        [*invitations, *messages], key=lambda r: as_utc(r[3]),
    ):
        key = f"failure:{agent_id}:{user_id}:{as_utc(at):%Y-%m-%d}"
        failures.setdefault(key, (org_id, agent_id, user_id, reason))
    if not failures:
        return 0
    async with session_factory() as db:
        told = {
            key.rsplit(":", 1)[0] for key in (
                await db.execute(
                    sa.select(OutreachMessageRow.dedupe_key)
                    .where(OutreachMessageRow.dedupe_key.like("failure:%"))
                    .where(OutreachMessageRow.created_at >= since)
                )
            ).scalars()
        }
    surfaced = 0
    for key, (org_id, agent_id, user_id, reason) in failures.items():
        if key in told:
            continue
        try:
            settings = await followup_store.get_settings(
                session_factory, org_id=org_id, agent_id=agent_id,
            )
            if followup_store.is_team_member(settings, user_id):
                continue
            name, phone, _zone = await describe_person(
                session_factory, org_id=org_id, agent_id=agent_id, user_id=user_id,
            )
            queued, _ = await followup_store.notify_team(
                session_factory, settings, org_id=org_id, agent_id=agent_id,
                kind="notice", created_by="outreach", subject_user_id=user_id,
                text=followup_store.render_text(
                    ((settings.texts or {}) if settings else {}).get("person_failure_notice"),
                    DEFAULT_TEXTS["person_failure_notice"],
                    name=name, why=plain_reason(reason), phone=phone,
                ),
                dedupe_prefix=key,
            )
            surfaced += queued
        except Exception:  # noqa: BLE001 — one person, not the pass
            logger.exception("[outreach] could not surface a failure for %s", user_id)
    return surfaced


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
    """One send pass over outreach.  Returns how many rows it moved.

    Reads queued rows, and held rows whose recipient has written since their
    template went out — the inbound path delivers those too, but not while a
    turn is running.  A held row still waiting for its answer is not re-read.
    """
    await _expire_unanswered(session_factory, now=now)
    await _report_outcomes(session_factory, now=now)
    await _remind_unseen_red_alerts(session_factory, now=now)
    await _surface_person_failures(session_factory, now=now)

    answered = sa.exists().where(
        ChannelContactRow.org_id == OutreachMessageRow.org_id,
        ChannelContactRow.platform == OutreachMessageRow.platform,
        ChannelContactRow.platform_user_id == OutreachMessageRow.platform_user_id,
        ChannelContactRow.channel_identifier == OutreachMessageRow.channel_identifier,
        ChannelContactRow.last_inbound_at > OutreachMessageRow.sent_at,
    )
    async with session_factory() as db:
        rows = (
            await db.execute(
                sa.select(OutreachMessageRow)
                .where(sa.or_(
                    OutreachMessageRow.state == "queued",
                    sa.and_(OutreachMessageRow.state == "awaiting_reply", answered),
                ))
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
    identity = await identity_lookup(row.org_id, row.platform, row.user_id)
    refusal = (
        f"No {row.platform} identity for the recipient" if identity is None
        else "No permission to message the recipient"
        if followup_store.permission_status(identity) != "granted" else None
    )
    if refusal:
        # A held row keeps waiting: its template already went out.
        return await _fail(session_factory, row.id, refusal) if queued else 0

    wa_id = identity.platform_user_id
    session_id = await dm_session(
        session_store, redis,
        session_factory=session_factory, storage=storage, settings=settings,
        org_id=row.org_id, agent_id=row.agent_id, user_id=row.user_id,
        platform=row.platform, session_key=whatsapp_session_key(wa_id),
        channel_id=wa_id, sender=row.channel_identifier,
    )
    last = await followup_store.last_inbound(
        session_factory, org_id=row.org_id, platform=row.platform,
        platform_user_id=wa_id, channel_identifier=row.channel_identifier,
    )
    if not window_open(last, now):
        if not queued:
            return 0
        return await _reopen(
            session_factory, row, session_store=session_store,
            delivery_service=delivery_service, session_id=session_id,
            wa_id=wa_id, now=now,
        )

    if await _busy(session_factory, session_id, now):
        return 0  # a turn is running in that conversation; next tick
    return int(await _deliver_now(
        session_factory, session_store, row, session_id=session_id, now=now,
        # A held text arriving because they answered is delivered; a queued
        # one written straight into an open window has only been sent.
        final_state="sent" if queued else "delivered",
    ))


async def _reopen(
    session_factory: Any,
    row: Any,
    *,
    session_store: Any,
    delivery_service: Any,
    session_id: uuid.UUID,
    wa_id: str,
    now: datetime,
) -> int:
    """The window is closed: hold the text and send the reopening template."""
    if not row.template_name or not row.template_language:
        return await _fail(
            session_factory, row.id,
            "The recipient's 24-hour window is closed and no approved "
            "template is configured to reopen it",
        )

    # The burst's template carries every text queued behind it.  An alert
    # rides only on an earlier alert: behind a routine notice it would reach
    # the team whenever they next happened to reply.
    earlier = (
        sa.select(OutreachMessageRow.id)
        .where(OutreachMessageRow.org_id == row.org_id)
        .where(OutreachMessageRow.agent_id == row.agent_id)
        .where(OutreachMessageRow.user_id == row.user_id)
        .where(OutreachMessageRow.platform == row.platform)
        .where(OutreachMessageRow.channel_identifier == row.channel_identifier)
        .where(OutreachMessageRow.state == "awaiting_reply")
        .where(OutreachMessageRow.outbox_id.isnot(None))
        .where(OutreachMessageRow.sent_at > now - BATCH_WINDOW)
        .order_by(OutreachMessageRow.sent_at.desc())
        .limit(1)
    )
    if row.kind == "alert":
        earlier = earlier.where(OutreachMessageRow.kind == "alert")
    async with session_factory() as db:
        carrier = (await db.execute(earlier)).scalar_one_or_none()
    if not await _transition(
        session_factory, row.id, from_states=("queued",),
        state="awaiting_reply", session_id=session_id, sent_at=now,
        platform_user_id=wa_id, rides_on=carrier,
    ):
        return 0
    if carrier is not None:
        return 1  # a template went out moments ago; this text rides on it

    try:
        event_id = await session_store.emit_synthetic_user_message(
            session_id,
            content=f"[Outreach opener sent: template '{row.template_name}']",
            synthetic=OUTREACH_OPENER_MARKER,
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
        await _transition(
            session_factory, row.id, from_states=("awaiting_reply",),
            state="queued", session_id=None, sent_at=None,
        )
        raise
    # The outbox row is how the dispatcher's verdict finds this template.
    await _transition(
        session_factory, row.id, from_states=("awaiting_reply",),
        outbox_id=int(outbox_id) if outbox_id is not None else None,
    )
    return 1


# ---------------------------------------------------------------------------
# Delivery results for reopening templates
# ---------------------------------------------------------------------------


async def _fail_reopened(db: Any, opener: Any, reason: str) -> int:
    """Fail a template's row and every text that rode on it."""
    result = await db.execute(
        sa.update(OutreachMessageRow)
        .where(sa.or_(
            OutreachMessageRow.id == opener.id,
            OutreachMessageRow.rides_on == opener.id,
        ))
        .where(OutreachMessageRow.state == "awaiting_reply")
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
            opener = await _opener_of_outbox(db, outbox_id)
            if opener is None:
                return False
            opener.outbox_id = outbox_id
        if error:
            # A template that never left is not a recipient who stayed
            # silent: say so, now, instead of 72 hours from now.
            await _fail_reopened(db, opener, f"The reopening template was not sent: {error}")
        elif provider_message_id:
            opener.provider_message_id = provider_message_id
        await db.commit()
    return True


async def _opener_of_outbox(db: Any, outbox_id: int) -> Any:
    """The outreach row a template's outbox row was written for.

    The dispatcher can claim and report the template before ``_reopen`` has
    stamped ``outbox_id`` on the row.  The outbox row delivers the opener
    event, and that event names its outreach row, so the report still finds
    it and a failed template fails its texts now, not 72 hours later.
    """
    event_id = await db.scalar(
        sa.select(DeliveryOutbox.event_id).where(DeliveryOutbox.id == outbox_id)
    )
    if event_id is None:
        return None
    marker = (
        await db.execute(
            sa.select(
                Event.data["synthetic"].astext, Event.data["outreach_id"].astext,
            ).where(Event.id == event_id)
        )
    ).first()
    if marker is None:
        return None
    synthetic, outreach_id = marker
    if synthetic != OUTREACH_OPENER_MARKER or not outreach_id:
        return None
    return await db.get(OutreachMessageRow, uuid.UUID(outreach_id))


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

