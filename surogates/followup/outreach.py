"""Get a message to one person without waiting for them to write first.

On any channel that can write first; a channel without a service window
(Slack, Telegram) takes the text straight into the person's conversation.
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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from surogates.channels.capabilities import capabilities_for
from surogates.channels.identity import mask_readable_phones, readable_phone
from surogates.db.models import (
    ChannelContactRow,
    ChannelIdentity,
    DeliveryOutbox,
    EscalationRow,
    ProgramInvitationRow,
    Event,
    FollowupEnrollmentRow,
    FollowupSettingsRow,
    OutreachMessageRow,
    User,
)
from surogates.followup import store as followup_store
from surogates.followup.eligibility import as_utc
from surogates.followup.wording import wording
from surogates.channels.identity import get_or_create_channel_session
from surogates.channels.source import SessionSource, build_session_key
from surogates.programs.opener import DmTarget, OpenerUndeliverable, dm_session, resolve_dm
from surogates.session.events import OUTREACH_OPENER_MARKER, PLATFORM_NOTE, EventType
from surogates.session.store import live_lease_token

logger = logging.getLogger(__name__)

__all__ = [
    "apply_outreach_status",
    "deliver_pending",
    "record_outreach_delivery",
    "record_outreach_text",
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
#: A message from Studio keeps the general wait.
OPERATOR_UNANSWERED_HOURS = 24
#: How far back the outcome pass looks.  A day longer than the longest wait
#: above or an agent may set, so every held text has settled inside it.
OUTCOME_LOOKBACK = max(
    UNANSWERED_AFTER,
    timedelta(hours=followup_store.SETTINGS_RANGES["operator_unanswered_hours"][1]),
) + timedelta(days=1)
#: How far back the reminder pass looks: past the longest reminder wait.
REMINDER_LOOKBACK = timedelta(
    minutes=followup_store.SETTINGS_RANGES["red_alert_reminder_minutes"][1],
) + timedelta(hours=12)
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
    "late_answer_notice": (
        "↩️ {name} answered after your message expired, so it was not "
        "delivered. Send it again? {phone}"
    ),
    "expired_message_to_person": (
        "A message from the team was waiting for your reply but expired "
        "before you answered. They know you have written back."
    ),
    # The {why} of the notices above: every reason the runtime records maps
    # to one of these, so a notice never quotes the platform's English.
    "why_window_closed": "their 24h WhatsApp window had closed",
    "why_no_permission": "no permission to message them",
    "why_template_failed": "WhatsApp refused the template",
    "why_expired": "no answer to the opening template",
    "why_no_template": "their 24h window is closed and no approved template is chosen",
    "why_no_opener": "the step has no message to open with",
    "why_no_number": "there is no number or account on record for them",
    "why_no_channel": "no channel the business has can reach them",
    "why_not_written_first": "they have not written to the business on that channel yet",
    "why_not_in_time": "it could not be sent in time",
    "why_no_phone_line": "the business has no phone line that can call out",
    "why_no_webhook": "the business's app connection is not set up",
    "why_setup": "the channel is not set up correctly for this business",
    "why_stopped": "the step was stopped before it was sent",
    "why_undelivered": "it could not be delivered",
    # Off unless the business words it.
    "busy_safety_line": "",
}
#: Rows handled per pass; the rest wait for the next tick.
BATCH = 200

_HELD = ("queued", "awaiting_reply")
_CREATED_BY = sa.func.coalesce(OutreachMessageRow.created_by, "")
_TEAM_REQUESTED = _CREATED_BY.like("team:%")
_UNANSWERED_REASON = "The recipient did not answer the template"


def window_open(last_inbound_at: datetime | None, now: datetime) -> bool:
    if last_inbound_at is None:
        return False
    return as_utc(now) - as_utc(last_inbound_at) < WINDOW - WINDOW_MARGIN


async def _busy(session_factory: Any, session_id: uuid.UUID, now: datetime) -> uuid.UUID | None:
    """The token of the turn holding this conversation right now, if any."""
    async with session_factory() as db:
        return await live_lease_token(db, session_id, now)


async def busy_line_for(
    session_factory: Any, *, org_id: uuid.UUID, agent_id: str, session_id: uuid.UUID,
    user_id: uuid.UUID,
) -> tuple[str, uuid.UUID] | None:
    """The business's ``busy_safety_line`` and the running turn's lease token,
    when *user_id* writes mid-turn; ``None`` when it is unset, no turn runs,
    or they are on the team (nobody waits on the agent for them)."""
    async with session_factory() as db:
        settings = await db.get(FollowupSettingsRow, (org_id, agent_id))
        line = wording(settings, "busy_safety_line").strip() if settings is not None else ""
        if not line or followup_store.is_team_member(settings, user_id):
            return None
        token = await live_lease_token(db, session_id)
    return (line, token) if token is not None else None


async def first_busy_line(redis: Any, session_id: uuid.UUID, token: uuid.UUID) -> bool:
    """Whether this is the running turn's first busy line: one per turn."""
    return bool(await redis.set(
        f"surogates:followup:busy-line:{session_id}:{token}", "1",
        ex=int(timedelta(hours=6).total_seconds()), nx=True,
    ))


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


#: Model-only notes on an outreach message in the log; never delivered.
MASKED_NOTE = (
    "[Numbers are masked in this log only; the recipient's copy shows them "
    "in full and can be tapped to call.]"
)
ESCALATION_NOTE = (
    "[Open escalation: when the team member takes it or says it is done, "
    "call team_escalation with action claim or resolve.]"
)


def _opener_note(row: Any) -> str:
    """What the model reads for a template it cannot see the text of."""
    note = f"[Outreach opener sent: template '{row.template_name}'"
    if row.subject_user_id is not None and row.subject_user_id != row.user_id:
        # A model once read the name under an alert as the team member's own.
        note += (
            ". This conversation is with a team member of the business; the "
            "message that follows is about another person, not about them"
        )
    return note + "]"


async def _write_text(
    session_store: Any, session_id: uuid.UUID, row: Any, text: str | None = None,
) -> None:
    # An assistant reply as far as the transcript and the outbox are
    # concerned, so it is delivered exactly as the agent's own replies are,
    # and the agent sees what was said when the person answers.  The log,
    # which the model reads, keeps phone numbers masked; only the outbox
    # carries them whole, for the team member who has to call.
    text = text or row.text
    logged = mask_readable_phones(text)
    # Without these the model, seeing only the masked log, told a team
    # member it could not give them the number and never closed the case.
    notes = [MASKED_NOTE] if logged != text else []
    if row.kind == "alert" and followup_store.escalation_of(row.dedupe_key):
        notes.append(ESCALATION_NOTE)
    data = {
        "message": {"role": "assistant", "content": logged},
        "synthetic": "outreach",
        "outreach_id": str(row.id),
        "outreach_kind": row.kind,
        **({PLATFORM_NOTE: "\n\n".join(notes)} if notes else {}),
    }
    await session_store.emit_event(
        session_id, EventType.LLM_RESPONSE, data,
        delivery_data={**data, "message": {"role": "assistant", "content": text}},
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
                .where(sa.or_(
                    OutreachMessageRow.state.in_(_HELD),
                    sa.and_(
                        OutreachMessageRow.kind == "operator",
                        OutreachMessageRow.state == "failed",
                        OutreachMessageRow.reason.like(f"{_UNANSWERED_REASON}%"),
                        OutreachMessageRow.sent_at >= now - OUTCOME_LOOKBACK,
                    ),
                ))
                .order_by(OutreachMessageRow.created_at)
            )
        ).scalars().all()
    if not rows or await _busy(session_factory, session_id, now):
        return 0

    delivered = 0
    agents = _Agents(session_factory)
    for row in rows:
        if row.state == "failed":
            await _answered_too_late(
                session_factory, session_store, row, session_id=session_id, agents=agents,
            )
        elif await _deliver_now(
            session_factory, session_store, row,
            session_id=session_id, now=now, final_state="delivered",
        ):
            delivered += 1
    return delivered


async def _answered_too_late(
    session_factory: Any, session_store: Any, row: Any, *, session_id: uuid.UUID, agents: _Agents,
) -> None:
    """Someone answered a template after the message behind it expired.

    Silence would read as being ignored: they are told the message expired,
    and whoever asked for it that they wrote back.  Once, by moving the row
    on from ``failed``.
    """
    if not await _transition(
        session_factory, row.id, from_states=("failed",), state="answered_late",
    ):
        return
    settings = await agents.settings(row.org_id, row.agent_id)
    await _write_text(
        session_store, session_id, row, wording(settings, "expired_message_to_person"),
    )
    requester = _requester(row.created_by)
    if requester is None:
        return
    name, phone, _zone = await describe_person(
        session_factory, org_id=row.org_id, agent_id=row.agent_id, user_id=row.user_id,
        zones=await agents.zones(row.org_id, row.agent_id), texts=settings,
    )
    await _tell_requester(
        session_factory, settings, row, requester,
        text=wording(settings, "late_answer_notice", name=name, phone=phone),
        dedupe_prefix=f"late:{row.id}",
    )


class _Agents:
    """Each agent's follow-up settings and protocol zones, read once a pass."""

    def __init__(self, session_factory: Any) -> None:
        self._sf = session_factory
        self._settings: dict[tuple, Any] = {}
        self._zones: dict[tuple, dict[str, str]] = {}

    async def settings(self, org_id: uuid.UUID, agent_id: str) -> Any:
        key = (org_id, agent_id)
        if key not in self._settings:
            self._settings[key] = await followup_store.get_settings(
                self._sf, org_id=org_id, agent_id=agent_id,
            )
        return self._settings[key]

    async def zones(self, org_id: uuid.UUID, agent_id: str) -> dict[str, str]:
        key = (org_id, agent_id)
        if key not in self._zones:
            self._zones[key] = await followup_store.protocol_timezones(
                self._sf, org_id=org_id, agent_id=agent_id,
            )
        return self._zones[key]

    async def describe(
        self, org_id: uuid.UUID, agent_id: str, user_id: uuid.UUID, described: Described,
    ) -> tuple[str, str, str]:
        return await describe_person(
            self._sf, org_id=org_id, agent_id=agent_id, user_id=user_id,
            zones=await self.zones(org_id, agent_id), described=described,
            texts=await self.settings(org_id, agent_id),
        )


async def _described(session_factory: Any, found: Any) -> dict[tuple, Described]:
    """Each ``(org_id, agent_id, user_id)`` of *found*, described one agent at a time."""
    by_agent: dict[tuple, set] = {}
    for org_id, agent_id, user_id in found:
        by_agent.setdefault((org_id, agent_id), set()).add(user_id)
    out: dict[tuple, Described] = {}
    for (org_id, agent_id), ids in by_agent.items():
        for user_id, described in (await describe_people(
            session_factory, org_id=org_id, agent_id=agent_id, user_ids=ids,
        )).items():
            out[(org_id, agent_id, user_id)] = described
    return out


async def _tell_requester(
    session_factory: Any, settings: Any, row: Any, requester: uuid.UUID, *,
    text: str, dedupe_prefix: str,
) -> tuple[int, str | None]:
    """A notice about *row* to the team member who asked for it, only."""
    return await followup_store.notify_team(
        session_factory, settings, org_id=row.org_id, agent_id=row.agent_id,
        kind="notice", created_by="outreach", subject_user_id=row.user_id,
        text=text, dedupe_prefix=dedupe_prefix, only_user_id=requester,
        fallback_senders={row.platform: row.channel_identifier},
    )


def operator_unanswered_hours(settings: Any) -> int:
    return int(getattr(settings, "operator_unanswered_hours", None) or OPERATOR_UNANSWERED_HOURS)


def _unanswered(hours: int) -> str:
    return f"{_UNANSWERED_REASON} within {hours} hours"


async def _expire_unanswered(session_factory: Any, *, now: datetime) -> int:
    """Give up on held texts whose template has gone unanswered too long."""
    held = (
        sa.update(OutreachMessageRow)
        .where(OutreachMessageRow.state == "awaiting_reply")
        .where(OutreachMessageRow.sent_at.isnot(None))
    )
    async with session_factory() as db:
        result = await db.execute(
            held.where(sa.not_(sa.and_(OutreachMessageRow.kind == "operator", _TEAM_REQUESTED)))
            .where(OutreachMessageRow.sent_at <= now - UNANSWERED_AFTER)
            .values(state="failed", reason=_unanswered(int(UNANSWERED_AFTER.total_seconds() // 3600)))
        )
        expired = int(result.rowcount or 0)
        # One statement per wait an agent has set: the waits are few, the
        # held messages may not be.
        wait = sa.func.coalesce(
            sa.select(FollowupSettingsRow.operator_unanswered_hours)
            .where(FollowupSettingsRow.org_id == OutreachMessageRow.org_id)
            .where(FollowupSettingsRow.agent_id == OutreachMessageRow.agent_id)
            .scalar_subquery(),
            OPERATOR_UNANSWERED_HOURS,
        )
        waits = set((
            await db.execute(
                sa.select(sa.distinct(FollowupSettingsRow.operator_unanswered_hours))
                .where(FollowupSettingsRow.operator_unanswered_hours.isnot(None))
            )
        ).scalars()) | {OPERATOR_UNANSWERED_HOURS}
        for hours in sorted(waits):
            result = await db.execute(
                held.where(OutreachMessageRow.kind == "operator")
                .where(_TEAM_REQUESTED)
                .where(OutreachMessageRow.sent_at <= now - timedelta(hours=hours))
                .where(wait == hours)
                .values(state="failed", reason=_unanswered(hours))
                .execution_options(synchronize_session=False)
            )
            expired += int(result.rowcount or 0)
        await db.commit()
        return expired


def _requester(created_by: str | None) -> uuid.UUID | None:
    """The team member who asked for a message, from ``team:<uid>``."""
    prefix, _, raw = (created_by or "").partition(":")
    if prefix != "team":
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


@dataclass
class Described:
    """A person as the team knows them: their enrollments, the name they go
    by, their WhatsApp number, and their ids and names on every channel."""

    user_id: uuid.UUID
    rows: list
    display_name: str
    wa_id: str
    #: ``{platform: [platform_user_id, username…]}``.
    handles: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.label()

    def label(self, texts: Any = None) -> str:
        """Their name as the team knows them, worded by *texts*."""
        return followup_store.person_label(
            self.display_name, followup_store.labelling_enrollment(self.rows), texts,
        )

    def zone(self, zones: dict[str, str]) -> str:
        enrollment = followup_store.labelling_enrollment(self.rows)
        return zones.get(getattr(enrollment, "protocol", None)) or next(iter(zones.values()), "UTC")


async def describe_people(
    session_factory: Any,
    *,
    org_id: uuid.UUID,
    agent_id: str,
    user_ids: Any = None,
    rows_by_user: dict[uuid.UUID, list] | None = None,
) -> dict[uuid.UUID, Described]:
    """Each person of *user_ids* (or of *rows_by_user*, their enrollments
    already read), described in at most three reads whatever their number."""
    if rows_by_user is None:
        ids = list(dict.fromkeys(user_ids or ()))
        rows_by_user = {uid: [] for uid in ids}
        if ids:
            async with session_factory() as db:
                found = (
                    await db.execute(
                        sa.select(FollowupEnrollmentRow)
                        .where(FollowupEnrollmentRow.org_id == org_id)
                        .where(FollowupEnrollmentRow.agent_id == agent_id)
                        .where(FollowupEnrollmentRow.user_id.in_(ids))
                        .order_by(FollowupEnrollmentRow.created_at)
                    )
                ).scalars().all()
            for row in found:
                rows_by_user[row.user_id].append(row)
    ids = list(rows_by_user)
    if not ids:
        return {}
    async with session_factory() as db:
        names = dict(
            (await db.execute(sa.select(User.id, User.display_name).where(User.id.in_(ids)))).all()
        )
        phones: dict[uuid.UUID, str] = {}
        handles: dict[uuid.UUID, dict[str, list[str]]] = {}
        for user_id, platform, pid, meta in (
            await db.execute(
                sa.select(
                    ChannelIdentity.user_id, ChannelIdentity.platform,
                    ChannelIdentity.platform_user_id, ChannelIdentity.platform_meta,
                )
                .where(ChannelIdentity.org_id == org_id)
                .where(ChannelIdentity.user_id.in_(ids))
            )
        ).all():
            if platform == "whatsapp":
                phones.setdefault(user_id, pid)
            names_there = [str((meta or {}).get(k) or "") for k in ("username", "display_name")]
            handles.setdefault(user_id, {}).setdefault(platform, []).extend(
                h for h in (pid, *names_there) if h
            )
    return {
        uid: Described(
            uid, rows, names.get(uid) or "", phones.get(uid) or "", handles.get(uid, {}),
        )
        for uid, rows in rows_by_user.items()
    }


async def describe_person(
    session_factory: Any,
    *,
    org_id: uuid.UUID,
    agent_id: str,
    user_id: uuid.UUID,
    zones: dict[str, str] | None = None,
    described: Described | None = None,
    texts: Any = None,
) -> tuple[str, str, str]:
    """(name, readable phone, timezone) of a person, as the team knows them."""
    if described is None:
        described = (await describe_people(
            session_factory, org_id=org_id, agent_id=agent_id, user_ids=[user_id],
        ))[user_id]
    if zones is None:
        zones = await followup_store.protocol_timezones(
            session_factory, org_id=org_id, agent_id=agent_id,
        )
    return (
        described.label(texts) or "?",
        readable_phone(described.wa_id) if described.wa_id else "?",
        described.zone(zones),
    )


#: How each recorded reason reads, first match wins: ``(fragments, key)``.
#: Anything else (a provider's own wording) is ``why_undelivered``.
_REASONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("did not answer the template",), "why_expired"),
    (("131047",), "why_window_closed"),
    (("no approved template", "no approved"), "why_no_template"),
    (("132015", "132016", "paused", "disabled", "rejected", "template was not"), "why_template_failed"),
    (("permission",), "why_no_permission"),
    (("no opener message",), "why_no_opener"),
    (("has not written to this", "never written"), "why_not_written_first"),
    (("within the response window", "no delivery report by the deadline"), "why_not_in_time"),
    (("call out",), "why_no_phone_line"),
    (("no webhook",), "why_no_webhook"),
    (("credential vault", "bot token", "opened no direct message"), "why_setup"),
    (("stopped before",), "why_stopped"),
    (("identity", "no number", "no whatsapp"), "why_no_number"),
    (("no reachable channel", "cannot write first", "no shared destination", "not switched on"),
     "why_no_channel"),
)


def plain_reason(reason: str | None, texts: dict | None = None) -> str:
    """Why a message did not arrive, in words a team member can act on.

    Each kind of reason is a ``why_*`` wording the agent's ``texts`` can
    override, so the team reads it in their own language; a reason the
    runtime does not recognise reads as ``why_undelivered``, never as the
    provider's own text.
    """
    lowered = (reason or "").lower()
    key = next(
        (k for fragments, k in _REASONS if any(f in lowered for f in fragments)),
        "why_undelivered",
    )
    return wording(texts, key)


async def _mark_reported(session_factory: Any, model: Any, ids: list, now: datetime) -> None:
    if not ids:
        return
    async with session_factory() as db:
        await db.execute(sa.update(model).where(model.id.in_(ids)).values(reported_at=now))
        await db.commit()


async def _report_outcomes(session_factory: Any, *, now: datetime) -> int:
    """Tell a team member what became of a message they asked for.

    One notice per message: a failure of anything they asked for (their
    message, the welcome of someone they enrolled, a notice to a colleague),
    and the delivery of their own message after a held wait.  A text
    written straight into an open window needs none: the tool already told
    them it went out.  The row is marked reported only once its notice is
    queued, and the notice is keyed on the row, so a pass that dies in
    between is retried without telling anyone twice.
    """
    async with session_factory() as db:
        rows = (
            await db.execute(
                sa.select(OutreachMessageRow)
                .where(OutreachMessageRow.reported_at.is_(None))
                .where(sa.or_(
                    OutreachMessageRow.state == "failed",
                    sa.and_(
                        OutreachMessageRow.kind == "operator",
                        OutreachMessageRow.state == "delivered",
                    ),
                ))
                .where(OutreachMessageRow.created_at >= now - OUTCOME_LOOKBACK)
                .where(_TEAM_REQUESTED)
                .order_by(OutreachMessageRow.created_at.desc())
                .limit(BATCH)
            )
        ).scalars().all()
    unasked = [row.id for row in rows if _requester(row.created_by) is None]
    await _mark_reported(session_factory, OutreachMessageRow, unasked, now)
    rows = [row for row in rows if row.id not in unasked]
    agents = _Agents(session_factory)
    people = await _described(
        session_factory, ((r.org_id, r.agent_id, r.user_id) for r in rows),
    )
    reported = 0
    for row in rows:
        requester = _requester(row.created_by)
        try:
            settings = await agents.settings(row.org_id, row.agent_id)
            name, phone, zone = await agents.describe(
                row.org_id, row.agent_id, row.user_id,
                people[(row.org_id, row.agent_id, row.user_id)],
            )
            if row.state == "delivered":
                at = as_utc(row.delivered_at or now).astimezone(ZoneInfo(zone))
                text = wording(settings, "delivered_notice", name=name, time=f"{at:%H:%M}", phone=phone)
            else:
                text = wording(
                    settings, "undelivered_notice",
                    name=name, why=plain_reason(row.reason, settings.texts if settings else None), phone=phone,
                )
            queued, _ = await _tell_requester(
                session_factory, settings, row, requester,
                text=text, dedupe_prefix=f"outcome:{row.id}",
            )
            await _mark_reported(session_factory, OutreachMessageRow, [row.id], now)
            reported += queued
        except Exception:  # noqa: BLE001 — one notice, not the pass
            logger.exception("[outreach] could not report the outcome of %s", row.id)
    return reported


def red_alert_reminder_minutes(settings: Any) -> int:
    minutes = getattr(settings, "red_alert_reminder_minutes", None) or RED_ALERT_REMINDER_MINUTES
    return max(int(minutes), int(BATCH_WINDOW.total_seconds() // 60))


async def _remind_unseen_red_alerts(session_factory: Any, *, now: datetime) -> int:
    """Send a red alert again, once, when no team member has read it.

    Read is WhatsApp's own read receipt on the alert's text, whether it went
    straight into an open window or followed a template; an answered
    template (the text then delivered) counts too.  A template read alone
    does not: the alert behind it is still unseen.  The reminder goes to the
    whole team, so a colleague can act when the first recipient is away
    from their phone.  Keyed on the escalation, it is one reminder whichever
    pass gets there.
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
                .where(OutreachMessageRow.dedupe_key.like(followup_store.ESCALATION_PREFIX + "%"))
                # A case a team member took or resolved needs no reminder.
                .where(~sa.exists().where(
                    EscalationRow.org_id == OutreachMessageRow.org_id,
                    EscalationRow.agent_id == OutreachMessageRow.agent_id,
                    sa.or_(EscalationRow.owner.isnot(None), EscalationRow.resolved_at.isnot(None)),
                    OutreachMessageRow.dedupe_key.like(
                        sa.literal(followup_store.ESCALATION_PREFIX + "%:")
                        + sa.func.replace(sa.cast(EscalationRow.id, sa.String), "-", "")
                        + sa.literal(":%")
                    ),
                ))
                .where(OutreachMessageRow.created_at <= now - BATCH_WINDOW)
                .where(OutreachMessageRow.created_at >= now - REMINDER_LOOKBACK)
                .order_by(OutreachMessageRow.created_at)
                .limit(BATCH)
            )
        ).all()
    groups: dict[str, list[tuple[Any, Any]]] = {}
    for row, settings in rows:
        groups.setdefault(followup_store.escalation_of(row.dedupe_key), []).append((row, settings))

    reminded = 0
    for escalation, members in groups.items():
        row, settings = members[0]
        wait = timedelta(minutes=red_alert_reminder_minutes(settings))
        if as_utc(row.created_at) > as_utc(now) - wait:
            continue
        if any(r.read_at is not None or r.state == "delivered" for r, _ in members):
            continue
        try:
            reminded += await _remind(session_factory, escalation, row, settings, now=now)
        except Exception:  # noqa: BLE001 — one escalation, not the pass
            logger.exception("[outreach] could not remind the team of %s", escalation)
    return reminded


async def _remind(
    session_factory: Any, escalation: str, row: Any, settings: Any, *, now: datetime,
) -> int:
    """Queue one escalation's reminder, then mark the escalation reminded.

    Queued first and keyed on the escalation, so a pass that dies before
    the mark retries without a second reminder, and one that dies before
    the queue never loses it.
    """
    minutes = red_alert_reminder_minutes(settings)
    queued, why_not = await followup_store.notify_team(
        session_factory, settings, org_id=row.org_id, agent_id=row.agent_id,
        kind="alert", severity="red", created_by="reminder", topic="urgent",
        text=wording(settings, "red_alert_reminder", minutes=minutes, text=row.text),
        subject_user_id=row.subject_user_id,
        dedupe_prefix="reminder:" + escalation.removeprefix(followup_store.ESCALATION_PREFIX),
        fallback_senders={row.platform: row.channel_identifier},
    )
    async with session_factory() as db:
        if queued:
            # A recipient still holding the alert behind its template gets
            # the reminder, which quotes it, instead: answering must not
            # deliver the same alert twice.
            await db.execute(
                sa.update(OutreachMessageRow)
                .where(OutreachMessageRow.org_id == row.org_id)
                .where(OutreachMessageRow.dedupe_key.like(f"{escalation}:%"))
                .where(OutreachMessageRow.state.in_(_HELD))
                .values(state="replaced", reason="Sent again as an unread red alert reminder")
            )
        claimed = await db.execute(
            sa.update(OutreachMessageRow)
            .where(OutreachMessageRow.org_id == row.org_id)
            .where(OutreachMessageRow.dedupe_key.like(f"{escalation}:%"))
            .where(OutreachMessageRow.reminded_at.is_(None))
            .values(reminded_at=now)
        )
        invitation_id = escalation.split(":")[1]
        if int(claimed.rowcount or 0) and invitation_id != "-":
            await db.execute(
                sa.update(ProgramInvitationRow)
                .where(ProgramInvitationRow.id == uuid.UUID(invitation_id))
                .values(reminder_state="reminded" if queued else "reminder_failed")
            )
        await db.commit()
    if not queued:
        logger.warning("[outreach] red alert reminder for %s reached nobody: %s", escalation, why_not)
    return queued if int(claimed.rowcount or 0) else 0


async def _surface_person_failures(session_factory: Any, *, now: datetime) -> int:
    """Tell the team when the agent or a Program could not reach a person.

    A check-in opener or a welcome that failed would otherwise only show in
    a run history nobody reads.  One notice per person per day of failure,
    whatever number of messages failed that day; the newest failures first,
    each marked reported once its notice is queued.  Whatever a team member
    asked for is reported to them by :func:`_report_outcomes` instead.
    """
    since = now - OUTCOME_LOOKBACK
    invitation_failed = sa.and_(
        ProgramInvitationRow.reported_at.is_(None),
        # Another channel is still to be tried: not a failure to report yet.
        ProgramInvitationRow.fallback_platforms.is_(None),
        sa.or_(
            ProgramInvitationRow.delivery_state.in_(("failed", "undelivered")),
            sa.and_(
                ProgramInvitationRow.delivery_state == "canceled",
                ProgramInvitationRow.reason.ilike("%permission%"),
            ),
        ),
        ProgramInvitationRow.created_at >= since,
    )
    message_failed = sa.and_(
        OutreachMessageRow.reported_at.is_(None),
        OutreachMessageRow.state == "failed",
        OutreachMessageRow.created_at >= since,
        sa.not_(_TEAM_REQUESTED),
    )
    # Most passes find nothing unreported: two index reads, and done.
    async with session_factory() as db:
        involved = set((
            await db.execute(
                sa.select(ProgramInvitationRow.org_id, ProgramInvitationRow.agent_id)
                .where(invitation_failed).distinct()
            )
        ).all()) | set((
            await db.execute(
                sa.select(OutreachMessageRow.org_id, OutreachMessageRow.agent_id)
                .where(message_failed).distinct()
            )
        ).all())
        if not involved:
            return 0
        teams = {
            (row.org_id, row.agent_id): row
            for row in (
                await db.execute(
                    sa.select(FollowupSettingsRow).where(
                        sa.tuple_(FollowupSettingsRow.org_id, FollowupSettingsRow.agent_id)
                        .in_(list(involved))
                    )
                )
            ).scalars()
            if row.alert_recipient_user_ids
        }
    if not teams:
        return 0
    async with session_factory() as db:
        invitations = (
            await db.execute(
                sa.select(
                    ProgramInvitationRow.id,
                    ProgramInvitationRow.org_id, ProgramInvitationRow.agent_id,
                    ProgramInvitationRow.user_id, ProgramInvitationRow.created_at,
                    sa.func.coalesce(ProgramInvitationRow.delivery_error, ProgramInvitationRow.reason),
                )
                .where(invitation_failed)
                .where(sa.tuple_(ProgramInvitationRow.org_id, ProgramInvitationRow.agent_id).in_(list(teams)))
                .order_by(ProgramInvitationRow.created_at.desc())
                .limit(BATCH)
            )
        ).all()
        messages = (
            await db.execute(
                sa.select(
                    OutreachMessageRow.id,
                    OutreachMessageRow.org_id, OutreachMessageRow.agent_id,
                    OutreachMessageRow.user_id, OutreachMessageRow.created_at,
                    OutreachMessageRow.reason,
                )
                .where(message_failed)
                .where(sa.tuple_(OutreachMessageRow.org_id, OutreachMessageRow.agent_id).in_(list(teams)))
                .order_by(OutreachMessageRow.created_at.desc())
                .limit(BATCH)
            )
        ).all()
    failures: dict[str, dict[str, Any]] = {}
    for model, found in ((ProgramInvitationRow, invitations), (OutreachMessageRow, messages)):
        for row_id, org_id, agent_id, user_id, at, reason in found:
            key = f"failure:{agent_id}:{user_id}:{as_utc(at):%Y-%m-%d}"
            failure = failures.setdefault(key, {
                "org_id": org_id, "agent_id": agent_id, "user_id": user_id,
                "first": (as_utc(at), reason), "rows": [],
            })
            failure["first"] = min(failure["first"], (as_utc(at), reason), key=lambda f: f[0])
            failure["rows"].append((model, row_id))

    agents = _Agents(session_factory)
    people = await _described(
        session_factory,
        ((f["org_id"], f["agent_id"], f["user_id"]) for f in failures.values()
         if not followup_store.is_team_member(teams[(f["org_id"], f["agent_id"])], f["user_id"])),
    )
    surfaced = 0
    for key, failure in failures.items():
        org_id, agent_id, user_id = failure["org_id"], failure["agent_id"], failure["user_id"]
        settings = teams[(org_id, agent_id)]
        try:
            if not followup_store.is_team_member(settings, user_id):
                name, phone, _zone = await agents.describe(
                    org_id, agent_id, user_id, people[(org_id, agent_id, user_id)],
                )
                queued, _ = await followup_store.notify_team(
                    session_factory, settings, org_id=org_id, agent_id=agent_id,
                    kind="notice", created_by="outreach", subject_user_id=user_id,
                    text=wording(
                        settings, "person_failure_notice",
                        name=name, why=plain_reason(failure["first"][1], settings.texts), phone=phone,
                    ),
                    dedupe_prefix=key, topic="delivery_failures",
                )
                surfaced += queued
            for model in (ProgramInvitationRow, OutreachMessageRow):
                await _mark_reported(
                    session_factory, model,
                    [row_id for m, row_id in failure["rows"] if m is model], now,
                )
        except Exception:  # noqa: BLE001 — one person, not the pass
            logger.exception("[outreach] could not surface a failure for %s", user_id)
    return surfaced


async def _refresh_holds(session_factory: Any, *, now: datetime) -> int:
    """Judge each held message's quiet hours again, in the person's zone as
    it is now: a zone changed since the message was queued moves its time,
    or sends it at once.  ``not_before`` is the estimate shown to whoever
    sent it, kept current.  Returns how many changed.
    """
    from surogates.programs.journey import quiet_until

    async with session_factory() as db:
        rows = (
            await db.execute(
                sa.select(OutreachMessageRow)
                .where(OutreachMessageRow.state == "queued")
                .where(OutreachMessageRow.not_before.isnot(None))
                .order_by(OutreachMessageRow.created_at)
                .limit(BATCH)
            )
        ).scalars().all()
    changed = 0
    for row in rows:
        held = await quiet_until(
            session_factory, org_id=row.org_id, agent_id=row.agent_id, user_id=row.user_id, now=now,
        )
        until = held[0] if held else None
        if until is not None and row.not_before is not None and as_utc(until) == as_utc(row.not_before):
            continue
        changed += await _transition(
            session_factory, row.id, from_states=("queued",), not_before=until,
        )
    return changed


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
    credentials_for: Any = None,
    slack_client_factory: Any = None,
) -> int:
    """One send pass over outreach.  Returns how many rows it moved.

    *credentials_for* and *slack_client_factory* are what a Slack message
    needs to open the direct message; see :func:`resolve_dm`.

    Reads queued rows, and held rows whose recipient has written since their
    template went out — the inbound path delivers those too, but not while a
    turn is running.  A held row still waiting for its answer is not re-read.
    """
    for bookkeeping in (
        _expire_unanswered, _report_outcomes, _remind_unseen_red_alerts,
        _surface_person_failures, _refresh_holds,
    ):
        try:
            await bookkeeping(session_factory, now=now)
        except Exception:  # noqa: BLE001 — never at the cost of the send pass
            logger.exception("[outreach] %s failed; sending anyway", bookkeeping.__name__)

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
                    sa.and_(
                        OutreachMessageRow.state == "queued",
                        # Held through the person's quiet hours.
                        sa.or_(
                            OutreachMessageRow.not_before.is_(None),
                            OutreachMessageRow.not_before <= now,
                        ),
                    ),
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
                credentials_for=credentials_for,
                slack_client_factory=slack_client_factory,
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
    credentials_for: Any = None,
    slack_client_factory: Any = None,
) -> int:
    queued = row.state == "queued"
    if row.destination:
        return await _send_to_destination(
            session_factory, row, session_store=session_store, redis=redis, now=now,
            storage=storage, settings=settings,
        )
    identity = await identity_lookup(row.org_id, row.platform, row.user_id)
    refusal = (
        f"No {row.platform} identity for the recipient" if identity is None
        else "No permission to message the recipient"
        if followup_store.permission_status(identity) != "granted" else None
    )
    if refusal:
        # A held row keeps waiting: its template already went out.
        return await _fail(session_factory, row.id, refusal) if queued else 0

    caps = capabilities_for(row.platform)
    user = identity.platform_user_id
    last = await followup_store.last_inbound(
        session_factory, org_id=row.org_id, platform=row.platform,
        platform_user_id=user, channel_identifier=row.channel_identifier,
    )
    if caps.needs_prior_contact and last is None:
        reason = f"The recipient has not written to this {row.platform} sender yet"
        return await _fail(session_factory, row.id, reason) if queued else 0
    try:
        target = await resolve_dm(
            row.platform, user, row.channel_identifier,
            credentials_for=credentials_for,
            slack_client_factory=slack_client_factory, org_id=row.org_id,
        )
    except OpenerUndeliverable as exc:
        return await _fail(session_factory, row.id, exc.reason) if queued else 0
    session_id = await dm_session(
        session_store, redis,
        session_factory=session_factory, storage=storage, settings=settings,
        org_id=row.org_id, agent_id=row.agent_id, user_id=row.user_id,
        platform=row.platform, session_key=target.session_key,
        channel_id=target.channel_id, sender=row.channel_identifier,
    )
    # Only a channel with a service window needs a template to reopen it;
    # anywhere else the text is written straight into the conversation.
    if caps.window_hours and not window_open(last, now):
        if not queued:
            return 0
        return await _reopen(
            session_factory, row, session_store=session_store,
            delivery_service=delivery_service, session_id=session_id,
            target=target, now=now,
        )

    if await _busy(session_factory, session_id, now):
        return 0  # a turn is running in that conversation; next tick
    return int(await _deliver_now(
        session_factory, session_store, row, session_id=session_id, now=now,
        # A held text arriving because they answered is delivered; a queued
        # one written straight into an open window has only been sent.
        final_state="sent" if queued else "delivered",
    ))


async def _send_to_destination(
    session_factory: Any, row: Any, *, session_store: Any, redis: Any, now: datetime,
    storage: Any, settings: Any,
) -> int:
    """Post into the team's shared place (a Slack or Discord channel, a Telegram group):
    the team configured it, so no person's permission or window applies."""
    channel = row.destination
    if row.platform not in ("slack", "telegram", "discord"):
        return await _fail(session_factory, row.id, f"No shared destination on {row.platform}")
    key = build_session_key(SessionSource(
        platform=row.platform, chat_id=channel, chat_type="channel", user_id=channel,
        user_name="", thread_id=None, chat_name=channel,
    ))
    session_id = await get_or_create_channel_session(
        session_store, redis, session_key=key, user_id=None, org_id=row.org_id,
        agent_id=row.agent_id, channel=row.platform,
        config={
            f"{row.platform}_channel_id": channel, f"{row.platform}_thread_key": None,
            "channel_identifier": row.channel_identifier, "multi_party": True,
            "memory_boundary": f"{row.platform}:team:{channel}",
        },
        session_factory=session_factory, storage=storage, settings=settings, resume=False,
    )
    if await _busy(session_factory, session_id, now):
        return 0
    return int(await _deliver_now(
        session_factory, session_store, row, session_id=session_id, now=now, final_state="sent",
    ))


async def _reopen(
    session_factory: Any,
    row: Any,
    *,
    session_store: Any,
    delivery_service: Any,
    session_id: uuid.UUID,
    target: DmTarget,
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
        platform_user_id=target.channel_id, rides_on=carrier,
    ):
        return 0
    if carrier is not None:
        return 1  # a template went out moments ago; this text rides on it

    try:
        event_id = await session_store.emit_synthetic_user_message(
            session_id,
            content=_opener_note(row),
            synthetic=OUTREACH_OPENER_MARKER,
            metadata={"outreach_id": str(row.id), "template_name": row.template_name},
        )
        outbox_id = await delivery_service.enqueue(
            session_id, event_id, row.platform, target.destination,
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


async def _opener_of_outbox(
    db: Any, outbox_id: int, marker: str = OUTREACH_OPENER_MARKER,
) -> Any:
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
    found = (
        await db.execute(
            sa.select(
                Event.data["synthetic"].astext, Event.data["outreach_id"].astext,
            ).where(Event.id == event_id)
        )
    ).first()
    if found is None:
        return None
    synthetic, outreach_id = found
    if synthetic != marker or not outreach_id:
        return None
    return await db.get(OutreachMessageRow, uuid.UUID(outreach_id))


async def record_outreach_text(
    session_factory: Any, outbox_id: int, *, provider_message_id: str,
) -> bool:
    """The provider's id for an outreach text the outbox sent, so a later
    ``read`` status can find it.  True when the row was an outreach text."""
    async with session_factory() as db:
        row = await _opener_of_outbox(db, outbox_id, marker="outreach")
        if row is None:
            return False
        row.text_message_id = provider_message_id
        await db.commit()
    return True


async def apply_outreach_status(
    session_factory: Any,
    *,
    provider_message_id: str,
    status: str,
    reason: str | None,
) -> bool:
    """A provider status for a reopening template or an outreach text.
    True when it was one."""
    async with session_factory() as db:
        opener = (
            await db.execute(
                sa.select(OutreachMessageRow)
                .where(sa.or_(
                    OutreachMessageRow.provider_message_id == provider_message_id,
                    OutreachMessageRow.text_message_id == provider_message_id,
                ))
            )
        ).scalars().first()
        if opener is None:
            return False
        if status == "read":
            # Only the text's own receipt: a template read leaves it unseen.
            if (
                opener.text_message_id == provider_message_id
                and opener.state in ("sent", "delivered")
                and opener.read_at is None
            ):
                opener.read_at = datetime.now(timezone.utc)
                await db.commit()
        elif status in ("failed", "undelivered") and opener.provider_message_id == provider_message_id:
            await _fail_reopened(
                db, opener,
                f"The reopening template was not delivered: {reason or status}",
            )
            await db.commit()
    return True

