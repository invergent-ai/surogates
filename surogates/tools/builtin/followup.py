"""Tools an agent uses to follow a person through a protocol, and to escalate.

Every tool acts on the person the conversation belongs to — the session's
user — never on someone named in the arguments.  That is what lets a patient
talk to these tools safely: there is no argument through which one person
could enrol, pause or read another.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
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
from surogates.followup.eligibility import day_index, start_date_problem
from surogates.tools.builtin.checkin import _open_invitation_for_session
from surogates.tools.registry import ToolSchema

logger = logging.getLogger(__name__)

#: Every tool this module registers; the worker withholds them all from
#: agents without the Programs capability.
FOLLOWUP_TOOL_NAMES: frozenset[str] = frozenset({
    "followup_enroll", "followup_update", "followup_status", "escalate",
})

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
                    "date. Give this or days_ago, not both; omit both if the "
                    "person does not know it yet."
                ),
            },
            "days_ago": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Day 0 as a number of days before today, when the person "
                    "answers that way (\"5 days ago\" is 5, \"yesterday\" is "
                    "1, \"today\" is 0). Counted in the protocol's timezone; "
                    "do not work out the date yourself."
                ),
            },
            "phase": {
                "type": "string",
                "description": "The phase to start in, when the protocol uses phases.",
            },
            "patient_name": {
                "type": "string",
                "description": (
                    "The patient's full name, as they or their relative gave "
                    "it. Ask for it: the WhatsApp profile name is whoever is "
                    "writing, which may be a relative."
                ),
            },
            "answered_by": {
                "type": "string",
                "description": (
                    "Who will answer the check-ins when it is not the patient, "
                    "with their relation, e.g. \"Ana, daughter\". Omit when "
                    "the patient answers."
                ),
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
        "start date or the patient's name, record who answers for them, move "
        "to another phase, or pause or end it (for example "
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
            "patient_name": {
                "type": "string",
                "description": "The corrected patient's full name.",
            },
            "answered_by": {
                "type": "string",
                "description": (
                    "Who now answers the check-ins for the patient, with their "
                    "relation, e.g. \"Ana, daughter\"."
                ),
            },
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
        "person you are talking to: patient, who answers for them, start date, "
        "the day they are on today, phase and status."
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
        #: The enrollment that names the patient, when there is one.
        self.enrollment: Any = None

    def person_label(self) -> str:
        """How the care team recognises who an alert is about."""
        name = followup_store.patient_label(self.display_name, self.enrollment)
        parts = [name] if name else []
        if self.channel == "whatsapp" and self.platform_user_id:
            parts.append(f"+{self.platform_user_id}")
        return " · ".join(parts) or f"user {self.user_id}"


async def _context(kwargs: dict) -> tuple[_Context | None, Any]:
    """The conversation's context and the agent's follow-up settings."""
    sf = kwargs["session_factory"]
    async with sf() as db:
        session = await db.get(SessionRow, uuid.UUID(str(kwargs["session_id"])))
        if session is None:
            return None, None
        user = await db.get(User, session.user_id) if session.user_id else None
        ctx = _Context(session, user)
    settings = await followup_store.get_settings(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id,
    )
    if ctx.user_id is not None:
        ctx.enrollment = followup_store.labelling_enrollment(
            await followup_store.enrollments(
                sf, org_id=ctx.org_id, agent_id=ctx.agent_id, user_id=ctx.user_id,
            )
        )
    return ctx, settings


def _parse_date(raw: Any) -> date | None:
    """A YYYY-MM-DD within the protocol horizon, or ValueError."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"start_date must be YYYY-MM-DD, got {text!r}") from exc
    problem = start_date_problem(parsed, datetime.now(timezone.utc).date())
    if problem:
        raise ValueError(problem)
    return parsed


def _start_date(arguments: dict, timezone_name: str) -> date | None:
    """Day 0 from ``start_date`` or ``days_ago``, never both.

    ``days_ago`` exists because people answer "five days ago", and a model
    left to turn that into a date reaches for a shell to do the arithmetic.
    It counts back from today where the protocol's Program counts days.
    """
    raw_days = arguments.get("days_ago")
    if raw_days is None or (isinstance(raw_days, str) and not raw_days.strip()):
        return _parse_date(arguments.get("start_date"))
    if str(arguments.get("start_date") or "").strip():
        raise ValueError("give start_date or days_ago, not both")
    if isinstance(raw_days, bool) or not str(raw_days).strip().isdigit():
        raise ValueError(f"days_ago must be a whole number of days, got {raw_days!r}")
    today = datetime.now(ZoneInfo(timezone_name)).date()
    return _parse_date((today - timedelta(days=int(str(raw_days).strip()))).isoformat())


async def _tell_care_team(sf: Any, ctx: _Context, settings: Any, *, kind: str, text: str):
    """The care team's phones, with who it is about, never the person talking."""
    return await followup_store.notify_care_team(
        sf, settings, org_id=ctx.org_id, agent_id=ctx.agent_id, kind=kind,
        text=f"{text.strip()}\n\n— {ctx.person_label()}", created_by="agent",
        subject_user_id=ctx.user_id, exclude_user_id=ctx.user_id,
        fallback_sender=ctx.sender if ctx.channel == "whatsapp" else None,
    )


def _told(queued: int, why_not: str | None) -> str:
    return (
        f"The care team was notified ({queued})."
        if queued else f"The care team was not notified: {why_not}."
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def handle_followup_enroll(arguments: dict, **kwargs: Any) -> str:
    sf = kwargs["session_factory"]
    ctx, settings = await _context(kwargs)
    if ctx is None or ctx.user_id is None:
        return "Enrolment needs a conversation with a known person."
    if ctx.channel != "whatsapp" or not ctx.platform_user_id:
        return (
            "Enrolment happens in the person's WhatsApp conversation, where "
            "the check-ins will reach them. Ask them to message the practice "
            "there."
        )
    if followup_store.is_care_team(settings, ctx.user_id):
        return (
            "Not enrolled: this person is on the care team (they receive the "
            "alerts). They are not a patient."
        )
    if arguments.get("consent") is not True:
        return (
            "Not enrolled: consent is required. Ask the person whether they "
            "agree to receive the practice's messages here, and call again "
            "with consent=true only if they say yes."
        )
    protocol = str(arguments.get("protocol") or "").strip()
    offered = await followup_store.protocol_timezones(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id,
    )
    if not offered:
        return (
            f"Not enrolled: the practice has no active Program, so nobody can "
            f"be enrolled in '{protocol}' or any other protocol. This is a "
            f"configuration problem at the practice, not something the person "
            f"did: tell them follow-up is not set up yet and the care team will "
            f"be told, then call escalate with the reason \"No active follow-up "
            f"Program for protocol '{protocol}'\"."
        )
    if protocol not in offered:
        return (
            f"Not enrolled: '{protocol}' is not offered here. Offered: "
            f"{', '.join(sorted(offered))}. If the practice told you to use "
            f"'{protocol}', it has no active Program for it: that is a "
            f"configuration problem at the practice, so escalate saying so."
        )
    summary = str(arguments.get("summary") or "").strip()
    if not summary:
        return "Not enrolled: write a short summary for the care team."
    try:
        start = _start_date(arguments, offered[protocol])
    except ValueError as exc:
        return f"Not enrolled: {exc}."
    phase = str(arguments.get("phase") or "").strip() or None
    patient_name = str(arguments.get("patient_name") or "").strip() or None
    answered_by = str(arguments.get("answered_by") or "").strip() or None

    granted = await followup_store.grant_contact_permission(
        sf, org_id=ctx.org_id, platform="whatsapp",
        platform_user_id=ctx.platform_user_id, recorded_by="agent",
    )
    if not granted:
        return "Not enrolled: this person has no WhatsApp identity on record."
    enrollment = await followup_store.upsert_enrollment(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id, user_id=ctx.user_id,
        protocol=protocol, start_date=start, phase=phase, enrolled_by="agent",
        patient_name=patient_name, answered_by=answered_by,
    )
    ctx.enrollment = enrollment
    queued, why_not = await _tell_care_team(sf, ctx, settings, kind="enrolment", text=summary)

    parts = [f"Enrolled in {protocol}"]
    if enrollment.patient_name:
        parts.append(f"for {enrollment.patient_name}")
    if enrollment.answered_by:
        parts.append(f"(answered by {enrollment.answered_by})")
    if enrollment.start_date:
        parts.append(f"from {enrollment.start_date.isoformat()}")
    if enrollment.phase:
        parts.append(f"phase {enrollment.phase}")
    return " ".join(parts) + ". Consent recorded. " + _told(queued, why_not)


async def handle_followup_update(arguments: dict, **kwargs: Any) -> str:
    sf = kwargs["session_factory"]
    ctx, settings = await _context(kwargs)
    if ctx is None or ctx.user_id is None:
        return "Updating an enrolment needs a conversation with a known person."
    protocol = str(arguments.get("protocol") or "").strip()
    changes: dict[str, Any] = {}
    # A blank optional field means "not given", never "erase": models fill
    # unused optional fields with "", and an erased start date silently
    # drops the person out of every day-windowed Program.
    if str(arguments.get("start_date") or "").strip():
        try:
            changes["start_date"] = _parse_date(arguments.get("start_date"))
        except ValueError as exc:
            return f"Not updated: {exc}."
    for field in ("phase", "patient_name", "answered_by"):
        if str(arguments.get(field) or "").strip():
            changes[field] = str(arguments[field]).strip()
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
    ctx.enrollment = row

    result = f"Updated {protocol}: status {row.status}"
    if row.patient_name:
        result += f", patient {row.patient_name}"
    if row.answered_by:
        result += f", answered by {row.answered_by}"
    if row.start_date:
        result += f", start {row.start_date.isoformat()}"
    if row.phase:
        result += f", phase {row.phase}"
    result += "."
    if status in ("paused", "ended"):
        queued, why_not = await _tell_care_team(
            sf, ctx, settings, kind="alert", text=f"{protocol} {status}: {reason}",
        )
        result += " " + _told(queued, why_not)
    return result


async def handle_followup_status(arguments: dict, **kwargs: Any) -> str:
    sf = kwargs["session_factory"]
    ctx, settings = await _context(kwargs)
    if ctx is None:
        return "No conversation context."
    zones = await followup_store.protocol_timezones(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id,
    )
    lines = [
        "Protocols offered: " + ", ".join(sorted(zones)) + "."
        if zones else
        "Protocols offered: none. The practice has no active Program, so "
        "nobody can be enrolled. This is a configuration problem at the "
        "practice, not something the person did: if they want follow-up, tell "
        "them it is not set up yet and call escalate with the reason \"No "
        "active follow-up Program\"."
    ]
    if ctx.user_id is None:
        return lines[0]
    if followup_store.is_care_team(settings, ctx.user_id):
        lines.append(
            "This person is on the care team: they receive the alerts and "
            "the practice's notices here. They are not a patient — do not "
            "enrol them or ask them check-in questions."
        )
    rows = await followup_store.enrollments(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id, user_id=ctx.user_id,
    )
    if not rows:
        lines.append("This person is not enrolled in any protocol.")
    for row in rows:
        today = datetime.now(ZoneInfo(zones.get(row.protocol, "UTC"))).date()
        index = day_index(row.start_date, today)
        detail = [f"{row.protocol}: {row.status}"]
        detail.append(
            f"patient {row.patient_name}" if row.patient_name
            else "patient's name not recorded"
        )
        if row.answered_by:
            detail.append(f"answered by {row.answered_by}")
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
        try:
            await db.commit()
            return True
        except IntegrityError:
            # The target is not a service account this database knows — a
            # human's id pasted where a principal belongs.
            await db.rollback()
            logger.warning(
                "[followup] escalation target %s for agent %s is not a "
                "service account", service_account_id, ctx.agent_id,
            )
            return False


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


async def handle_escalate(arguments: dict, **kwargs: Any) -> str:
    reason = str(arguments.get("reason") or "").strip()
    if not reason:
        return "reason is required — say what needs the care team's attention."

    sf = kwargs["session_factory"]
    ctx, settings = await _context(kwargs)
    if ctx is None:
        return "Could not escalate: no conversation context."

    invitation = await _open_invitation_for_session(sf, ctx.session_id)
    program_target = None
    if invitation is not None:
        async with sf() as db:
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

    # Inside a check-in the Program's operator; outside one the agent's.
    target_id = _uuid_or_none(
        program_target or (settings.escalation_service_account_id if settings else None)
    )
    payload: dict[str, Any] = (
        {"program_id": str(invitation.program_id),
         "occurrence_id": str(invitation.occurrence_id)}
        if invitation is not None else {}
    )
    inboxed = target_id is not None and await _inbox(
        sf, ctx, service_account_id=target_id,
        event_type="checkin.escalation" if invitation is not None else "followup.escalation",
        reason=reason, payload=payload,
    )
    queued, why_not = await _tell_care_team(sf, ctx, settings, kind="alert", text=reason)

    if invitation is not None:
        # One write records what happened, on the check-in's own history.
        if inboxed:
            state, note = "raised", None
        elif queued:
            state = "raised"
            note = (
                "Inbox target is not a valid service account; care-team phones notified"
                if target_id is not None else "No inbox target; care-team phones notified"
            )
        else:
            # A visible failure, never a silent one: the agent saying it
            # escalated is not evidence that anyone was told.
            state, note = "failed", "No escalation target or care-team phone configured"
        async with sf() as db:
            row = await db.get(ProgramInvitationRow, invitation.id)
            row.escalation_state = state
            if note:
                row.reason = note
            await db.commit()

    if not inboxed and not queued:
        return (
            "Could not escalate: nobody is configured to receive this "
            f"({why_not}). Tell the person to contact the practice directly."
        )
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
