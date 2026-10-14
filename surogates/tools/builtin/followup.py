"""Tools an agent uses to follow a person through a protocol, and to escalate.

Every tool acts on the person the conversation belongs to — the session's
user — never on someone named in the arguments.  That is what lets anyone
talk to these tools safely: there is no argument through which one person
could enrol, pause or read another.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from surogates.db.models import (
    Event,
    InboxItem,
    ProgramInvitationRow,
    ProgramScheduleRow,
)
from surogates.followup import store as followup_store
from surogates.followup.context import (
    Context,
    load_context,
    local_time,
    parse_start_date,
    start_date_from,
    start_text,
    told,
)
from surogates.followup.eligibility import day_index, next_contact
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
        "business's scheduled check-ins start reaching them. Only call this "
        "after the person has clearly agreed to be contacted on this channel; "
        "pass consent=true to record that agreement. Enrolling again updates "
        "the start date or phase and reactivates a paused or ended enrolment. "
        "The team is told about every enrolment."
    ),
    parameters={
        "type": "object",
        "properties": {
            "protocol": {
                "type": "string",
                "description": (
                    "The protocol slug. Call followup_status first to see the "
                    "protocols this business offers."
                ),
            },
            "start_date": {
                "type": "string",
                "description": (
                    "Day 0 of the protocol as YYYY-MM-DD, e.g. the day the "
                    "programme started. Give this or days_ago, not both. Without either "
                    "the person is not enrolled unless date_unknown is true."
                ),
            },
            "days_ago": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Day 0 as a number of days before today, when the person "
                    "answers that way (\"5 days ago\" is 5, \"yesterday\" is "
                    "1, \"today\" is 0). Counted in the protocol's timezone; "
                    "do not work out the date yourself. For a rough period "
                    "(\"about two weeks\", \"a month ago\") pass the nearest "
                    "whole number of days (14, 30) with "
                    "start_date_approximate=true."
                ),
            },
            "start_date_approximate": {
                "type": "boolean",
                "description": (
                    "True when the person only knew the start roughly, so the "
                    "team knows to confirm it."
                ),
            },
            "date_unknown": {
                "type": "boolean",
                "description": (
                    "True only when the person really cannot say when day 0 "
                    "was, even roughly. No Program reaches them until a date "
                    "is set, so ask for days_ago first."
                ),
            },
            "phase": {
                "type": "string",
                "description": "The phase to start in, when the protocol uses phases.",
            },
            "person_name": {
                "type": "string",
                "description": (
                    "The followed person's full name, as they or whoever "
                    "writes for them gave "
                    "it. Ask for it: the WhatsApp profile name is whoever is "
                    "writing, which may be a relative. Required unless "
                    "name_unknown is true."
                ),
            },
            "name_unknown": {
                "type": "boolean",
                "description": (
                    "True only when the person would not give the followed person's "
                    "name after you asked."
                ),
            },
            "answered_by": {
                "type": "string",
                "description": (
                    "Who will answer the check-ins when it is not the person followed, "
                    "with their relation, e.g. \"Ana, daughter\". Omit when "
                    "the person answers themselves."
                ),
            },
            "consent": {
                "type": "boolean",
                "description": (
                    "True only when the person agreed, in this conversation, "
                    "to receive messages from the business here."
                ),
            },
            "summary": {
                "type": "string",
                "description": (
                    "One or two sentences for the team about this person "
                    "and this enrolment, in the team's language."
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
        "start date or the person's name, record who answers for them, move "
        "to another phase, or pause or end it (for example "
        "while they are away). Pausing or ending tells the team, "
        "with your reason. Only change what the person or the business has "
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
            "start_date_approximate": {
                "type": "boolean",
                "description": (
                    "Whether the start date is only approximate. A corrected "
                    "start_date counts as exact unless you pass true."
                ),
            },
            "phase": {"type": "string", "description": "The new phase."},
            "person_name": {
                "type": "string",
                "description": "The corrected full name of the person followed.",
            },
            "answered_by": {
                "type": "string",
                "description": (
                    "Who now answers the check-ins for the person, with their "
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
                    "Why, in the team's language. Required when pausing "
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
        "Show the protocols this business offers and the enrolments of the "
        "person you are talking to: whose follow-up it is, who answers for them, start date, "
        "the day they are on today, phase and status."
    ),
    parameters={"type": "object", "properties": {}},
)

ESCALATE_SCHEMA = ToolSchema(
    name="escalate",
    description=(
        "Raise something to the team now. Use it whenever what the "
        "person says needs a human: something the protocol lists, "
        "something the protocol does not cover, distress, or a request only "
        "the business can answer. It reaches the responsible operator's inbox "
        "and the team's phones. It does not end a check-in; record the "
        "outcome separately."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": (
                    "What needs attention, quoting the person where it "
                    "matters, in one to three sentences the team can "
                    "act on, in their language."
                ),
            },
            "severity": {
                "type": "string",
                "description": (
                    "red when you gave the person an emergency script (urgent "
                    "instructions to get help now); yellow otherwise. "
                    "Default yellow."
                ),
            },
        },
        "required": ["reason"],
    },
)


async def _tell_team(
    sf: Any, ctx: Context, settings: Any, *, kind: str, text: str, **extra: Any,
):
    """The team's phones, with who it is about, never the person talking."""
    extra.setdefault("reason", text)
    return await followup_store.notify_team(
        sf, settings, org_id=ctx.org_id, agent_id=ctx.agent_id, kind=kind,
        text=f"{text.strip()}\n\n— {await ctx.person_label(sf)}", created_by="agent",
        subject_user_id=ctx.user_id, exclude_user_id=ctx.user_id,
        fallback_sender=ctx.sender if ctx.channel == "whatsapp" else None,
        **extra,
    )


async def _last_inbound(sf: Any, ctx: Context) -> datetime | None:
    """When the person last wrote in this conversation's WhatsApp window."""
    if ctx.channel == "whatsapp" and ctx.platform_user_id and ctx.sender:
        return await followup_store.last_inbound(
            sf, org_id=ctx.org_id, platform="whatsapp",
            platform_user_id=ctx.platform_user_id, channel_identifier=ctx.sender,
        )
    return None


def _next_contact_text(row: Any, configs: list[dict], last: datetime | None) -> str:
    """When the Programs will next write to this person, in the Program's zone.

    Computed, never assumed: someone enrolled on day 40 is past every
    first-month Program, and "tomorrow at 08:30" would be a broken promise.
    """
    if row.status != "active":
        return f"No scheduled message: the enrolment is {row.status}."
    found = next_contact(
        configs, protocol=row.protocol, start_date=row.start_date,
        phase=row.phase, enrolled_at=row.created_at, last_inbound_at=last,
        now=datetime.now(timezone.utc),
    )
    if found is None:
        return (
            "No scheduled message ahead: no active Program reaches this person "
            "at their start date and phase. Do not promise them a time."
        )
    at, config = found
    name = str(config.get("name") or "").strip()
    return (
        f"Next scheduled message: {local_time(at, config)}"
        + (f" (Program '{name}')" if name else "")
        + ". When you tell the person, use this time, never one of your own."
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def with_person_name(arguments: dict) -> dict:
    """``person_name`` is the argument; ``patient_name`` is its older name."""
    name = arguments.get("person_name")
    return {**arguments, "patient_name": name} if name else arguments


async def handle_followup_enroll(arguments: dict, **kwargs: Any) -> str:
    arguments = with_person_name(arguments)
    sf = kwargs["session_factory"]
    ctx, settings = await load_context(kwargs)
    if ctx is None or ctx.user_id is None:
        return "Enrolment needs a conversation with a known person."
    if ctx.channel != "whatsapp" or not ctx.platform_user_id:
        return (
            "Enrolment happens in the person's WhatsApp conversation, where "
            "the check-ins will reach them. Ask them to message the business "
            "there."
        )
    if followup_store.is_team_member(settings, ctx.user_id):
        return (
            "Not enrolled: this person is on the team (they receive the "
            "alerts). They are not someone followed."
        )
    if arguments.get("consent") is not True:
        return (
            "Not enrolled: consent is required. Ask the person whether they "
            "agree to receive the business's messages here, and call again "
            "with consent=true only if they say yes."
        )
    protocol = str(arguments.get("protocol") or "").strip()
    configs = await followup_store.active_program_configs(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id,
    )
    offered = await followup_store.protocol_timezones(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id, configs=configs,
    )
    if not offered:
        return (
            f"Not enrolled: the business has no active Program, so nobody can "
            f"be enrolled in '{protocol}' or any other protocol. This is a "
            f"configuration problem at the business, not something the person "
            f"did: tell them follow-up is not set up yet and the team will "
            f"be told, then call escalate with the reason \"No active follow-up "
            f"Program for protocol '{protocol}'\"."
        )
    if protocol not in offered:
        return (
            f"Not enrolled: '{protocol}' is not offered here. Offered: "
            f"{', '.join(sorted(offered))}. If the business told you to use "
            f"'{protocol}', it has no active Program for it: that is a "
            f"configuration problem at the business, so escalate saying so."
        )
    summary = str(arguments.get("summary") or "").strip()
    if not summary:
        return "Not enrolled: write a short summary for the team."
    try:
        start = start_date_from(arguments, offered[protocol])
    except ValueError as exc:
        return f"Not enrolled: {exc}."
    missing = _enrol_missing(arguments)
    if missing:
        return "Not enrolled: " + " ".join(missing)
    phase = str(arguments.get("phase") or "").strip() or None
    approximate = arguments.get("start_date_approximate") is True if start else None
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
        start_date_approximate=approximate,
    )
    ctx.named_by(enrollment)
    queued, why_not = await _tell_team(sf, ctx, settings, kind="enrolment", text=summary)

    parts = [f"Enrolled in {protocol}"]
    if enrollment.patient_name:
        parts.append(f"for {enrollment.patient_name}")
    if enrollment.answered_by:
        parts.append(f"(answered by {enrollment.answered_by})")
    if enrollment.start_date:
        parts.append(f"from {start_text(enrollment)}")
    if enrollment.phase:
        parts.append(f"phase {enrollment.phase}")
    if enrollment.start_date is None:
        return (
            " ".join(parts) + ". Consent recorded. " + told(queued, why_not)
            + " No start date is recorded, so no Program will message this "
            "person until the date is set. Call escalate so the team can "
            "set it, and do not promise the person any message."
        )
    return (
        " ".join(parts) + ". Consent recorded. " + told(queued, why_not)
        + " " + _next_contact_text(enrollment, configs, await _last_inbound(sf, ctx))
    )


def _enrol_missing(arguments: dict) -> list[str]:
    """What a model left out that would leave the enrolment useless.

    Models drop optional arguments; a person enrolled without a start date
    is never reached by a day-windowed Program, so silence must be explicit.
    """
    missing = []
    has_date = any(
        str(arguments.get(key) if arguments.get(key) is not None else "").strip()
        for key in ("start_date", "days_ago")
    )
    if not has_date and arguments.get("date_unknown") is not True:
        missing.append(
            "pass days_ago (e.g. 30 with start_date_approximate: true for "
            "'about a month'), start_date as YYYY-MM-DD, or date_unknown: true "
            "if the person really doesn't know."
        )
    if (
        not str(arguments.get("patient_name") or "").strip()
        and arguments.get("name_unknown") is not True
    ):
        missing.append(
            "pass person_name (the followed person's full name; ask for it if you "
            "do not have it), or name_unknown: true if the person will not "
            "give it."
        )
    return missing


async def handle_followup_update(arguments: dict, **kwargs: Any) -> str:
    arguments = with_person_name(arguments)
    sf = kwargs["session_factory"]
    ctx, settings = await load_context(kwargs)
    if ctx is None or ctx.user_id is None:
        return "Updating an enrolment needs a conversation with a known person."
    protocol = str(arguments.get("protocol") or "").strip()
    changes: dict[str, Any] = {}
    # A blank optional field means "not given", never "erase": models fill
    # unused optional fields with "", and an erased start date silently
    # drops the person out of every day-windowed Program.
    if str(arguments.get("start_date") or "").strip():
        try:
            changes["start_date"] = parse_start_date(arguments.get("start_date"))
        except ValueError as exc:
            return f"Not updated: {exc}."
    if isinstance(arguments.get("start_date_approximate"), bool):
        changes["start_date_approximate"] = arguments["start_date_approximate"]
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
    ctx.named_by(row)

    result = f"Updated {protocol}: status {row.status}"
    if row.patient_name:
        result += f", person_name {row.patient_name}"
    if row.answered_by:
        result += f", answered by {row.answered_by}"
    if row.start_date:
        result += f", start {start_text(row)}"
    if row.phase:
        result += f", phase {row.phase}"
    result += "."
    if status in ("paused", "ended"):
        queued, why_not = await _tell_team(
            sf, ctx, settings, kind="alert", text=f"{protocol} {status}: {reason}",
        )
        result += " " + told(queued, why_not)
    return result


async def handle_followup_status(arguments: dict, **kwargs: Any) -> str:
    sf = kwargs["session_factory"]
    ctx, settings = await load_context(kwargs)
    if ctx is None:
        return "No conversation context."
    configs = await followup_store.active_program_configs(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id,
    )
    zones = await followup_store.protocol_timezones(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id, configs=configs,
    )
    lines = [
        "Protocols offered: " + ", ".join(sorted(zones)) + "."
        if zones else
        "Protocols offered: none. The business has no active Program, so "
        "nobody can be enrolled. This is a configuration problem at the "
        "business, not something the person did: if they want follow-up, tell "
        "them it is not set up yet and call escalate with the reason \"No "
        "active follow-up Program\"."
    ]
    if ctx.user_id is None:
        return lines[0]
    if followup_store.is_team_member(settings, ctx.user_id):
        lines.append(
            "This person is on the team: they receive the alerts and "
            "the business's notices here. They are not someone followed — do not "
            "enrol them or ask them check-in questions."
        )
    rows = await followup_store.enrollments(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id, user_id=ctx.user_id,
    )
    if not rows:
        lines.append("This person is not enrolled in any protocol.")
    last = await _last_inbound(sf, ctx) if rows else None
    for row in rows:
        today = datetime.now(ZoneInfo(zones.get(row.protocol, "UTC"))).date()
        index = day_index(row.start_date, today)
        detail = [f"{row.protocol}: {row.status}"]
        detail.append(
            f"person_name {row.patient_name}" if row.patient_name
            else "person_name not recorded"
        )
        if row.answered_by:
            detail.append(f"answered by {row.answered_by}")
        detail.append(
            f"start {start_text(row)} (day {index} today)"
            if row.start_date else "no start date"
        )
        if row.phase:
            detail.append(f"phase {row.phase}")
        if row.status_reason:
            detail.append(f"reason: {row.status_reason}")
        lines.append(
            ", ".join(detail) + ". "
            + _next_contact_text(row, configs, last)
        )
    return "\n".join(lines)


async def _inbox(
    sf: Any,
    ctx: Context,
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
                title=f"Needs your attention: {await ctx.person_label(sf)}",
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


_RED = frozenset({"red", "rosu", "roșu", "urgent", "critical"})


def _severity(raw: Any) -> str:
    """red or yellow, whatever the model wrote: an escalation is never
    refused over its colour, and anything unclear is the milder one."""
    return "red" if str(raw or "").strip().lower() in _RED else "yellow"


async def handle_escalate(arguments: dict, **kwargs: Any) -> str:
    reason = str(arguments.get("reason") or "").strip()
    if not reason:
        return "reason is required — say what needs the team's attention."
    severity = _severity(arguments.get("severity"))

    sf = kwargs["session_factory"]
    ctx, settings = await load_context(kwargs)
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
    # The key groups this escalation's alerts and its event, so an unopened
    # red one is reminded once and read as one, and names the check-in
    # whose history records it.
    escalation = f"escalation:{invitation.id if invitation is not None else '-'}:{uuid.uuid4().hex}"
    inboxed = target_id is not None and await _inbox(
        sf, ctx, service_account_id=target_id,
        event_type="checkin.escalation" if invitation is not None else "followup.escalation",
        reason=reason, payload={**payload, "escalation": escalation},
    )
    queued, why_not = await _tell_team(
        sf, ctx, settings, kind="alert",
        text=f"🔴 {reason}" if severity == "red" else reason, reason=reason,
        severity=severity, dedupe_prefix=escalation,
    )

    if invitation is not None:
        # One write records what happened, on the check-in's own history.
        if inboxed:
            state, note = "raised", None
        elif queued:
            state = "raised"
            note = (
                "Inbox target is not a valid service account; team phones notified"
                if target_id is not None else "No inbox target; team phones notified"
            )
        else:
            # A visible failure, never a silent one: the agent saying it
            # escalated is not evidence that anyone was told.
            state, note = "failed", "No escalation target or team phone configured"
        async with sf() as db:
            row = await db.get(ProgramInvitationRow, invitation.id)
            row.escalation_state = state
            if note:
                row.reason = note
            await db.commit()

    if not inboxed and not queued:
        return (
            "Could not escalate: nobody is configured to receive this "
            f"({why_not}). Tell the person to contact the business directly."
        )
    told = []
    if inboxed:
        told.append("the responsible operator's inbox")
    if queued:
        told.append(f"{queued} team phone(s)")
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
