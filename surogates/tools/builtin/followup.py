"""Tools an agent uses to follow a person through a protocol, and to escalate.

Every tool acts on the person the conversation belongs to — the session's
user — never on someone named in the arguments.  That is what lets a patient
talk to these tools safely: there is no argument through which one person
could enrol, pause or read another.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from surogates.db.models import (
    Event,
    InboxItem,
    ProgramInvitationRow,
    ProgramScheduleRow,
    Session as SessionRow,
    User,
)
from surogates.followup import store as followup_store
from surogates.followup.eligibility import MAX_DAY, Audience, day_index
from surogates.tools.registry import ToolSchema

logger = logging.getLogger(__name__)

#: Every tool this module registers; the worker withholds them all from
#: agents without the Programs capability.
FOLLOWUP_TOOL_NAMES: frozenset[str] = frozenset({
    "followup_enroll", "followup_update", "followup_status", "escalate",
})

#: Response states a check-in can still be escalated from.
_OPEN_CHECKIN = ("replied", "in_progress")

FOLLOWUP_ENROLL_SCHEMA = ToolSchema(
    name="followup_enroll",
    description=(
        "Enrol the person you are talking to in a follow-up protocol, so the "
        "practice's scheduled check-ins start reaching them. Only call this "
        "after the person has clearly agreed to be contacted on this channel; "
        "pass consent=true to record that agreement. Enrolling again updates "
        "the start date or phase and reactivates a paused or ended enrolment. "
        "The care team is told about every enrolment."
    ),
    parameters={
        "type": "object",
        "properties": {
            "protocol": {
                "type": "string",
                "description": (
                    "The protocol slug. Call followup_status first to see the "
                    "protocols this practice offers."
                ),
            },
            "start_date": {
                "type": "string",
                "description": (
                    "Day 0 of the protocol as YYYY-MM-DD, e.g. the procedure "
                    "date. Omit it if the person does not know it yet."
                ),
            },
            "phase": {
                "type": "string",
                "description": "The phase to start in, when the protocol uses phases.",
            },
            "consent": {
                "type": "boolean",
                "description": (
                    "True only when the person agreed, in this conversation, "
                    "to receive messages from the practice here."
                ),
            },
            "summary": {
                "type": "string",
                "description": (
                    "One or two sentences for the care team about this person "
                    "and this enrolment, in the care team's language."
                ),
            },
        },
        "required": ["protocol", "consent", "summary"],
    },
)

FOLLOWUP_UPDATE_SCHEMA = ToolSchema(
    name="followup_update",
    description=(
        "Change the enrolment of the person you are talking to: correct the "
        "start date, move to another phase, or pause or end it (for example "
        "while they are in hospital). Pausing or ending tells the care team, "
        "with your reason. Only change what the person or the practice has "
        "told you; never move a phase on your own judgement."
    ),
    parameters={
        "type": "object",
        "properties": {
            "protocol": {"type": "string", "description": "The protocol slug."},
            "start_date": {
                "type": "string",
                "description": "The corrected day 0, as YYYY-MM-DD.",
            },
            "phase": {"type": "string", "description": "The new phase."},
            "status": {
                "type": "string",
                "enum": list(followup_store.ENROLLMENT_STATUSES),
                "description": "active, paused or ended.",
            },
            "reason": {
                "type": "string",
                "description": (
                    "Why, in the care team's language. Required when pausing "
                    "or ending."
                ),
            },
        },
        "required": ["protocol"],
    },
)

FOLLOWUP_STATUS_SCHEMA = ToolSchema(
    name="followup_status",
    description=(
        "Show the protocols this practice offers and the enrolments of the "
        "person you are talking to: start date, the day they are on today, "
        "phase and status."
    ),
    parameters={"type": "object", "properties": {}},
)

ESCALATE_SCHEMA = ToolSchema(
    name="escalate",
    description=(
        "Raise something to the care team now. Use it whenever what the "
        "person says needs a human: a symptom or reading the protocol lists, "
        "something the protocol does not cover, distress, or a request only "
        "the practice can answer. It reaches the responsible operator's inbox "
        "and the care team's phones. It does not end a check-in; record the "
        "outcome separately."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": (
                    "What needs attention, quoting the person where it "
                    "matters, in one to three sentences the care team can "
                    "act on, in their language."
                ),
            },
        },
        "required": ["reason"],
    },
)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


class _Context:
    """Who and where the conversation is, read from the session itself."""

    def __init__(self, session: SessionRow, user: User | None) -> None:
        self.session = session
        self.session_id: uuid.UUID = session.id
        self.org_id: uuid.UUID = session.org_id
        self.agent_id: str = session.agent_id
        self.user_id: uuid.UUID | None = session.user_id
        self.channel: str = session.channel or ""
        config = session.config or {}
        self.sender: str = str(config.get("channel_identifier") or "")
        self.platform_user_id: str = str(
            config.get(f"{self.channel}_channel_id") or ""
        )
        self.display_name: str = (user.display_name if user else "") or ""

    def person_label(self) -> str:
        """How the care team recognises who an alert is about."""
        parts = [self.display_name.strip()] if self.display_name.strip() else []
        if self.channel == "whatsapp" and self.platform_user_id:
            parts.append(f"+{self.platform_user_id}")
        return " · ".join(parts) or f"user {self.user_id}"


async def _context(kwargs: dict) -> _Context | None:
    sf = kwargs["session_factory"]
    async with sf() as db:
        session = await db.get(SessionRow, uuid.UUID(str(kwargs["session_id"])))
        if session is None:
            return None
        user = await db.get(User, session.user_id) if session.user_id else None
        return _Context(session, user)


def _parse_date(raw: Any) -> date | None:
    """A YYYY-MM-DD within the protocol horizon, or ValueError."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"start_date must be YYYY-MM-DD, got {text!r}") from exc
    if abs((parsed - datetime.now(timezone.utc).date()).days) > MAX_DAY:
        raise ValueError("start_date is too far from today to be a protocol start")
    return parsed


async def _protocol_timezones(sf: Any, org_id: uuid.UUID, agent_id: str) -> dict[str, str]:
    """Each offered protocol's timezone, taken from a Program that targets it."""
    async with sf() as db:
        configs = (
            await db.execute(
                sa.select(ProgramScheduleRow.config)
                .where(ProgramScheduleRow.org_id == org_id)
                .where(ProgramScheduleRow.agent_id == agent_id)
                .where(ProgramScheduleRow.active.is_(True))
            )
        ).scalars().all()
    zones: dict[str, str] = {}
    for config in configs:
        try:
            audience = Audience.from_config((config or {}).get("audience"))
        except ValueError:
            continue
        if audience is not None:
            zones.setdefault(audience.protocol, str(config.get("timezone") or "UTC"))
    return zones


async def _notify_care_team(
    sf: Any, ctx: _Context, *, kind: str, text: str,
) -> tuple[int, str | None]:
    """Queue *text* for every alert recipient.  Returns (queued, why-not)."""
    settings = await followup_store.get_settings(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id,
    )
    if settings is None or not settings.alert_recipient_user_ids:
        return 0, "no care-team phones are configured"
    sender = settings.channel_identifier or (
        ctx.sender if ctx.channel == "whatsapp" else ""
    )
    if not sender:
        return 0, "no WhatsApp sender is configured for alerts"

    body = f"{text.strip()}\n\n— {ctx.person_label()}"
    queued = 0
    for recipient in settings.alert_recipient_user_ids:
        try:
            recipient_id = uuid.UUID(str(recipient))
        except ValueError:
            logger.warning("[followup] ignoring malformed alert recipient %r", recipient)
            continue
        if recipient_id == ctx.user_id:
            continue  # the care team member is the one talking; they know
        await followup_store.queue_outreach(
            sf, org_id=ctx.org_id, agent_id=ctx.agent_id, user_id=recipient_id,
            platform="whatsapp", channel_identifier=sender, kind=kind,
            text=body, template_name=settings.alert_template_name,
            template_language=settings.alert_template_language,
            subject_user_id=ctx.user_id, source_session_id=ctx.session_id,
            created_by="agent",
        )
        queued += 1
    return queued, None if queued else "the only configured recipient is this person"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def handle_followup_enroll(arguments: dict, **kwargs: Any) -> str:
    sf = kwargs["session_factory"]
    ctx = await _context(kwargs)
    if ctx is None or ctx.user_id is None:
        return "Enrolment needs a conversation with a known person."
    if ctx.channel != "whatsapp" or not ctx.platform_user_id:
        return (
            "Enrolment happens in the person's WhatsApp conversation, where "
            "the check-ins will reach them. Ask them to message the practice "
            "there."
        )
    if arguments.get("consent") is not True:
        return (
            "Not enrolled: consent is required. Ask the person whether they "
            "agree to receive the practice's messages here, and call again "
            "with consent=true only if they say yes."
        )
    protocol = str(arguments.get("protocol") or "").strip()
    offered = await followup_store.offered_protocols(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id,
    )
    if protocol not in offered:
        listing = ", ".join(sorted(offered)) or "none"
        return f"Not enrolled: '{protocol}' is not offered here. Offered: {listing}."
    summary = str(arguments.get("summary") or "").strip()
    if not summary:
        return "Not enrolled: write a short summary for the care team."
    try:
        start = _parse_date(arguments.get("start_date"))
    except ValueError as exc:
        return f"Not enrolled: {exc}."
    phase = str(arguments.get("phase") or "").strip() or None

    granted = await followup_store.grant_contact_permission(
        sf, org_id=ctx.org_id, platform="whatsapp",
        platform_user_id=ctx.platform_user_id, recorded_by="agent",
    )
    if not granted:
        return "Not enrolled: this person has no WhatsApp identity on record."
    enrollment = await followup_store.upsert_enrollment(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id, user_id=ctx.user_id,
        protocol=protocol, start_date=start, phase=phase, enrolled_by="agent",
    )
    queued, why_not = await _notify_care_team(sf, ctx, kind="enrolment", text=summary)

    parts = [f"Enrolled in {protocol}"]
    if enrollment.start_date:
        parts.append(f"from {enrollment.start_date.isoformat()}")
    if enrollment.phase:
        parts.append(f"phase {enrollment.phase}")
    told = (
        f"The care team was notified ({queued})."
        if queued else f"The care team was not notified: {why_not}."
    )
    return " ".join(parts) + ". Consent recorded. " + told


async def handle_followup_update(arguments: dict, **kwargs: Any) -> str:
    sf = kwargs["session_factory"]
    ctx = await _context(kwargs)
    if ctx is None or ctx.user_id is None:
        return "Updating an enrolment needs a conversation with a known person."
    protocol = str(arguments.get("protocol") or "").strip()
    changes: dict[str, Any] = {}
    if "start_date" in arguments:
        try:
            changes["start_date"] = _parse_date(arguments.get("start_date"))
        except ValueError as exc:
            return f"Not updated: {exc}."
    if "phase" in arguments:
        changes["phase"] = str(arguments.get("phase") or "").strip() or None
    status = arguments.get("status")
    reason = str(arguments.get("reason") or "").strip()
    if status is not None:
        if status not in followup_store.ENROLLMENT_STATUSES:
            return (
                "Not updated: status must be one of "
                f"{', '.join(followup_store.ENROLLMENT_STATUSES)}."
            )
        if status in ("paused", "ended") and not reason:
            return "Not updated: say why when pausing or ending an enrolment."
        changes["status"] = status
        changes["status_reason"] = reason or None
    if not changes:
        return "Nothing to update."

    row = await followup_store.update_enrollment(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id, user_id=ctx.user_id,
        protocol=protocol, **changes,
    )
    if row is None:
        return f"Not updated: this person is not enrolled in '{protocol}'."

    result = f"Updated {protocol}: status {row.status}"
    if row.start_date:
        result += f", start {row.start_date.isoformat()}"
    if row.phase:
        result += f", phase {row.phase}"
    result += "."
    if status in ("paused", "ended"):
        queued, why_not = await _notify_care_team(
            sf, ctx, kind="alert", text=f"{protocol} {status}: {reason}",
        )
        result += (
            f" The care team was notified ({queued})."
            if queued else f" The care team was not notified: {why_not}."
        )
    return result


async def handle_followup_status(arguments: dict, **kwargs: Any) -> str:
    sf = kwargs["session_factory"]
    ctx = await _context(kwargs)
    if ctx is None:
        return "No conversation context."
    zones = await _protocol_timezones(sf, ctx.org_id, ctx.agent_id)
    lines = [
        "Protocols offered: " + (", ".join(sorted(zones)) or "none") + "."
    ]
    if ctx.user_id is None:
        return lines[0]
    rows = await followup_store.enrollments_for_user(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id, user_id=ctx.user_id,
    )
    if not rows:
        lines.append("This person is not enrolled in any protocol.")
    for row in rows:
        today = datetime.now(ZoneInfo(zones.get(row.protocol, "UTC"))).date()
        index = day_index(row.start_date, today)
        detail = [f"{row.protocol}: {row.status}"]
        detail.append(
            f"start {row.start_date.isoformat()} (day {index} today)"
            if row.start_date else "no start date"
        )
        if row.phase:
            detail.append(f"phase {row.phase}")
        if row.status_reason:
            detail.append(f"reason: {row.status_reason}")
        lines.append(", ".join(detail) + ".")
    return "\n".join(lines)


async def _inbox(
    sf: Any,
    ctx: _Context,
    *,
    service_account_id: uuid.UUID,
    event_type: str,
    reason: str,
    payload: dict,
    invitation_id: uuid.UUID | None,
) -> bool:
    """Raise a hand in the operator's inbox.  False when the target is invalid."""
    async with sf() as db:
        # inbox_items.source_event_id is NOT NULL and unique, so the
        # escalation is first an event on the person's own session.
        event = Event(
            session_id=ctx.session_id, org_id=ctx.org_id, type=event_type,
            data={"reason": reason, **payload},
        )
        db.add(event)
        await db.flush()
        db.add(
            InboxItem(
                org_id=ctx.org_id,
                # The designated operator, never the acting principal: an
                # item raised against the person reaches nobody but them.
                user_id=None,
                service_account_id=service_account_id,
                session_id=ctx.session_id,
                source_event_id=event.id,
                # An existing InboxKind; the ops Inbox filters on a Literal.
                kind="action_required",
                title=f"Needs your attention: {ctx.person_label()}",
                body=reason,
                payload={**payload, "user_id": str(ctx.user_id)},
            )
        )
        if invitation_id is not None:
            row = await db.get(ProgramInvitationRow, invitation_id)
            # Escalation is its own axis: raising a hand does not mean the
            # person finished answering, so response_state is untouched.
            row.escalation_state = "raised"
        try:
            await db.commit()
            return True
        except IntegrityError:
            # The target is not a service account this database knows — a
            # human's id pasted where a principal belongs.  The insert rolled
            # back, event included.
            await db.rollback()
            logger.warning(
                "[followup] escalation target %s for agent %s is not a "
                "service account", service_account_id, ctx.agent_id,
            )
            return False


async def handle_escalate(arguments: dict, **kwargs: Any) -> str:
    reason = str(arguments.get("reason") or "").strip()
    if not reason:
        return "reason is required — say what needs the care team's attention."

    sf = kwargs["session_factory"]
    ctx = await _context(kwargs)
    if ctx is None:
        return "Could not escalate: no conversation context."

    async with sf() as db:
        invitation = (
            await db.execute(
                sa.select(ProgramInvitationRow)
                .where(ProgramInvitationRow.session_id == ctx.session_id)
                .where(ProgramInvitationRow.response_state.in_(_OPEN_CHECKIN))
                .order_by(ProgramInvitationRow.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        program_target = None
        if invitation is not None:
            sched = (
                await db.execute(
                    sa.select(ProgramScheduleRow).where(
                        ProgramScheduleRow.program_id == invitation.program_id
                    )
                )
            ).scalar_one_or_none()
            program_target = ((sched.config if sched else None) or {}).get(
                "escalation_service_account_id"
            )

    settings = await followup_store.get_settings(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id,
    )
    target = program_target or (
        settings.escalation_service_account_id if settings else None
    )
    try:
        target_id = uuid.UUID(str(target)) if target else None
    except ValueError:
        target_id = None

    payload: dict[str, Any] = {}
    if invitation is not None:
        payload = {
            "program_id": str(invitation.program_id),
            "occurrence_id": str(invitation.occurrence_id),
        }
    inboxed = False
    if target_id is not None:
        inboxed = await _inbox(
            sf, ctx, service_account_id=target_id,
            event_type=(
                "checkin.escalation" if invitation is not None
                else "followup.escalation"
            ),
            reason=reason, payload=payload,
            invitation_id=invitation.id if invitation is not None else None,
        )

    queued, why_not = await _notify_care_team(sf, ctx, kind="alert", text=reason)

    if not inboxed and not queued:
        if invitation is not None:
            # A visible failure, never a silent one: the agent saying it
            # escalated is not evidence that anyone was told.
            async with sf() as db:
                row = await db.get(ProgramInvitationRow, invitation.id)
                row.escalation_state = "failed"
                row.reason = "No escalation target or care-team phone configured"
                await db.commit()
        return (
            "Could not escalate: nobody is configured to receive this "
            f"({why_not}). Tell the person to contact the practice directly."
        )

    if invitation is not None and not inboxed:
        # Someone was told, by phone; the check-in's history must say so, and
        # why the inbox was not part of it.
        async with sf() as db:
            row = await db.get(ProgramInvitationRow, invitation.id)
            row.escalation_state = "raised"
            row.reason = (
                "Inbox target is not a valid service account; care-team "
                "phones notified" if target_id is not None
                else "No inbox target; care-team phones notified"
            )
            await db.commit()

    told = []
    if inboxed:
        told.append("the responsible operator's inbox")
    if queued:
        told.append(f"{queued} care-team phone(s)")
    return "Escalated to " + " and ".join(told) + "."


def register(registry: Any) -> None:
    """Register the follow-up and escalation tools."""
    for name, schema, handler in (
        ("followup_enroll", FOLLOWUP_ENROLL_SCHEMA, handle_followup_enroll),
        ("followup_update", FOLLOWUP_UPDATE_SCHEMA, handle_followup_update),
        ("followup_status", FOLLOWUP_STATUS_SCHEMA, handle_followup_status),
        ("escalate", ESCALATE_SCHEMA, handle_escalate),
    ):
        registry.register(
            name=name, schema=schema, handler=handler, toolset="followup",
        )
