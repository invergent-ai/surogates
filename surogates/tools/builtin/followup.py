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

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from surogates.channels.capabilities import initiating_platforms
from surogates.db.models import (
    Event,
    InboxItem,
    ProgramInvitationRow,
    ProgramScheduleRow,
)
from surogates.channels.identity import mask_readable_phones, normalize_wa_id
from surogates.followup import people
from surogates.followup import store as followup_store
from surogates.followup.context import (
    Context,
    channel_names,
    in_person_zone,
    load_context,
    local_time,
    parse_start_date,
    start_date_from,
    start_text,
    told,
)
from surogates.followup.eligibility import day_today, next_contact
from surogates.session.interactive_input import fold_label
from surogates.tools.builtin.checkin import _open_invitation_for_session
from surogates.tools.builtin.followup_people import (
    CONTACT_TOOL_NAMES,
    FOLLOWUP_CONTACTS_SCHEMA,
    FOLLOWUP_WITHDRAW_CONSENT_SCHEMA,
    add_contact,
    handle_followup_contacts,
    handle_followup_withdraw_consent,
)
from surogates.tools.registry import ToolSchema

logger = logging.getLogger(__name__)

#: Every tool this module registers; the worker withholds them all from
#: agents without the Programs capability.
FOLLOWUP_TOOL_NAMES: frozenset[str] = frozenset({
    "followup_enroll", "followup_update", "followup_status", "escalate",
    *CONTACT_TOOL_NAMES,
})

FOLLOWUP_ENROLL_SCHEMA = ToolSchema(
    name="followup_enroll",
    description=(
        "Enrol a person in a follow-up protocol, so the business's scheduled "
        "messages start reaching them: the one writing, or (for_someone_else) "
        "someone they write for, such as a parent, who then becomes a person "
        "of their own with the one writing linked as their contact. Every "
        "step goes to the person and to each linked contact. Only call this "
        "after the one writing has clearly agreed to be contacted on this "
        "channel; pass consent=true to record that agreement. Enrolling again "
        "updates the start date or phase and reactivates a paused or ended "
        "enrolment. The team is told about every enrolment."
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
                    "it. Ask for it: the profile name on the channel is whoever is "
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
                    "Who will answer the scheduled messages when it is not the person followed, "
                    "with their relation, e.g. \"Ana, daughter\". Omit when "
                    "the person answers themselves."
                ),
            },
            "for_someone_else": {
                "type": "boolean",
                "description": (
                    "True when the one writing enrols someone else (a parent, a "
                    "child): that person is followed, the one writing is linked "
                    "as their contact (pass relation)."
                ),
            },
            "relation": {
                "type": "string",
                "description": (
                    "With for_someone_else: who the one writing is to the person, "
                    "e.g. \"daughter\"."
                ),
            },
            "person_phone": {
                "type": "string",
                "description": (
                    "With for_someone_else: the person's own number, when they have "
                    "one. They are messaged there only after they agree themselves."
                ),
            },
            "contacts": {
                "type": "array",
                "description": (
                    "Other people to link to the person (numbers the one writing "
                    "gives you), each receiving every step once they agree."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "phone": {"type": "string"},
                        "name": {"type": "string"},
                        "relation": {"type": "string"},
                    },
                    "required": ["phone", "relation"],
                },
            },
            "consent": {
                "type": "boolean",
                "description": (
                    "True only when the one writing agreed, in this conversation, "
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
        "told you; never move a phase on your own judgement. When the date "
        "the journey counts from moves (an appointment is "
        "rescheduled), set start_date to the new date: every step, including "
        "those before it (negative days), follows the new date from now on; "
        "steps already sent stay as they were."
    ),
    parameters={
        "type": "object",
        "properties": {
            "protocol": {"type": "string", "description": "The protocol slug."},
            "start_date": {
                "type": "string",
                "description": (
                    "Day 0, the date the journey counts from, as YYYY-MM-DD: "
                    "a correction or a rescheduled date. May be in the future."
                ),
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
                    "Who now answers the scheduled messages for the person, with their "
                    "relation, e.g. \"Ana, daughter\"."
                ),
            },
            "timezone": {
                "type": "string",
                "description": (
                    "The person's own IANA timezone (e.g. Europe/London) when they "
                    "live in another zone than the business: steps follow it."
                ),
            },
            "preferred_channel": {
                "type": "string",
                "enum": ["whatsapp", "telegram", "slack"],
                "description": "The channel the person asked to be written to on.",
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
        "and the team. Escalate every new report of a sign, even when a "
        "similar one was escalated before, in this conversation or an earlier "
        "one: the team needs to know it is still happening, and the platform "
        "groups repeats. It does not end a scheduled step; record the "
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
                    "act on, in their language. Do not state the severity "
                    "or its colour: the severity field carries it and the "
                    "platform marks red alerts."
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
            "to": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Names of the team members this is for, chosen from what each "
                    "handles (followup_status lists them). Omit to tell everyone "
                    "who receives this kind of alert."
                ),
            },
        },
        "required": ["reason"],
    },
)


async def _tell_team(
    sf: Any, ctx: Context, settings: Any, *, kind: str, text: str, **extra: Any,
):
    """The team, with who it is about, never the person talking."""
    extra.setdefault("reason", text)
    return await followup_store.notify_team(
        sf, settings, org_id=ctx.org_id, agent_id=ctx.agent_id, kind=kind,
        text=followup_store.signed(text, await ctx.person_label(sf, settings), settings),
        created_by="agent",
        subject_user_id=await ctx.person_user_id(sf), exclude_user_id=ctx.user_id,
        fallback_senders={ctx.channel: ctx.sender},
        **extra,
    )


async def _last_inbound(sf: Any, ctx: Context) -> datetime | None:
    """When the person last wrote to this conversation's sender."""
    if ctx.can_write_first:
        return await followup_store.last_inbound(
            sf, org_id=ctx.org_id, platform=ctx.channel,
            platform_user_id=await ctx.person_id(sf), channel_identifier=ctx.sender,
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
        in_person_zone(configs, row), protocol=row.protocol, start_date=row.start_date,
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
    if not ctx.can_write_first:
        channels = channel_names(sorted(initiating_platforms()))
        return (
            "Enrolment happens in the person's own conversation on a channel "
            f"the business can message first ({channels}), where the scheduled "
            "messages will reach them. Ask them to message the business there."
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
    if not offered or protocol not in offered:
        return (
            f"Not enrolled: '{protocol}' is not active for this business"
            + (f" (active: {', '.join(sorted(offered))})" if offered else "")
            + ". The person is waiting on the team now: call escalate with what "
            f"they asked for and the reason \"No active follow-up for '{protocol}'\". "
            "This note is for you: do not explain programs, protocols or "
            "configuration to the person; tell them only that the team will get "
            "back to them."
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
    someone_else = arguments.get("for_someone_else") is True
    relation = str(arguments.get("relation") or "").strip()
    if someone_else and (not relation or not patient_name):
        return (
            "Not enrolled: for someone else, pass relation (who the one writing "
            "is to them) and person_name (their full name)."
        )

    granted = await followup_store.grant_contact_permission(
        sf, org_id=ctx.org_id, platform=ctx.channel,
        platform_user_id=await ctx.person_id(sf), recorded_by="agent",
    )
    if not granted:
        return f"Not enrolled: this person has no {ctx.channel} identity on record."
    person = ctx.user_id
    if someone_else:
        person = await _person_for(sf, ctx, settings, patient_name, arguments.get("person_phone"))
        if isinstance(person, str):
            return f"Not enrolled: {person}"
        answered_by = None
    enrollment = await followup_store.upsert_enrollment(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id, user_id=person,
        protocol=protocol, start_date=start, phase=phase, enrolled_by="agent",
        patient_name=patient_name, answered_by=answered_by,
        start_date_approximate=approximate,
    )
    if someone_else:
        await followup_store.link_contact(
            sf, org_id=ctx.org_id, agent_id=ctx.agent_id, person_user_id=person,
            contact_user_id=ctx.user_id, relation=relation, created_by="agent",
        )
    linked = [
        await add_contact(sf, ctx, settings, person, enrollment.patient_name or "", item)
        for item in (arguments.get("contacts") or []) if isinstance(item, dict)
    ]
    ctx.named_by(enrollment)
    queued, why_not = await _tell_team(
        sf, ctx, settings, kind="enrolment", text=summary, topic="new_enrolments",
    )

    parts = [f"Enrolled in {protocol}"]
    if enrollment.patient_name:
        parts.append(f"for {enrollment.patient_name}")
    if enrollment.answered_by:
        parts.append(f"(answered by {enrollment.answered_by})")
    if enrollment.start_date:
        parts.append(f"from {start_text(enrollment)}")
    if enrollment.phase:
        parts.append(f"phase {enrollment.phase}")
    if someone_else:
        parts.append(f"with the one writing linked as their {relation}")
    if linked:
        parts.append("(" + " ".join(linked) + ")")
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


async def _person_for(
    sf: Any, ctx: Context, settings: Any, name: str, phone: Any,
) -> uuid.UUID | str:
    """The person someone enrols on their behalf: the one they already speak
    for under that name, the account holding the number they gave, else a new
    person known by name."""
    for link in await followup_store.contacts(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id, contact_user_id=ctx.user_id,
    ):
        known = await followup_store.person_name(
            sf, org_id=ctx.org_id, agent_id=ctx.agent_id, user_id=link.person_user_id,
        )
        if fold_label(known) == fold_label(name):
            return link.person_user_id
    if str(phone or "").strip():
        try:
            wa_id = normalize_wa_id(str(phone))
        except ValueError as exc:
            return str(exc)
        holder = await people.channel_holder(sf, ctx.org_id, "whatsapp", wa_id)
        if holder is not None and (
            holder.id == ctx.user_id or followup_store.is_team_member(settings, holder.id)
        ):
            return "that number is the one writing's own, or a team member's."
        return await people.channel_account(
            sf, org_id=ctx.org_id, agent_id=ctx.agent_id, platform="whatsapp",
            platform_user_id=wa_id, name=name, holder=holder,
        )
    return await people.named_account(sf, org_id=ctx.org_id, agent_id=ctx.agent_id, name=name)


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
    for field in ("phase", "patient_name", "answered_by", "timezone", "preferred_channel"):
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

    try:
        row = await followup_store.update_enrollment(
            sf, org_id=ctx.org_id, agent_id=ctx.agent_id, user_id=await ctx.person_user_id(sf),
            protocol=protocol, **changes,
        )
    except ValueError as exc:
        return f"Not updated: {exc}."
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
    if row.timezone:
        result += f", timezone {row.timezone}"
    if row.preferred_channel:
        result += f", preferred channel {row.preferred_channel}"
    result += "."
    # Already so (a stop the platform honoured first): the team was told.
    if status in ("paused", "ended") and row.previous_status != status:
        journey = await followup_store.journey_names(
            sf, org_id=ctx.org_id, agent_id=ctx.agent_id, protocols=[protocol],
        )
        queued, why_not = await _tell_team(
            sf, ctx, settings, kind="alert",
            text=followup_store.wording(settings, f"notice_{status}", journey=journey, reason=reason),
            topic="attention",
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
            "enrol them or ask them a step's questions."
        )
    speaker = await ctx.speaker(sf)
    if speaker is not None:
        lines.append(followup_store.writer_line(speaker) + ".")
        links = await followup_store.contacts(
            sf, org_id=ctx.org_id, agent_id=ctx.agent_id, person_user_id=speaker.person_user_id,
        )
        names = await followup_store.display_names(sf, [c.contact_user_id for c in links])
        lines.append(
            "Linked contacts (each receives every step; any answer counts for "
            "all): " + ("; ".join(
                f"{names.get(c.contact_user_id) or '?'} ({c.relation or 'contact'})" for c in links
            ) or "none") + "."
        )
    team = _team_lines(settings, await followup_store.display_names(
        sf, [uuid.UUID(str(u)) for u in (settings.alert_recipient_user_ids if settings else [])],
    ))
    if team:
        lines.append("Team (for escalate.to, by what they handle): " + "; ".join(team) + ".")
    rows = await followup_store.enrollments(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id, user_id=await ctx.person_user_id(sf),
    )
    if not rows:
        lines.append("This person is not enrolled in any protocol.")
    last = await _last_inbound(sf, ctx) if rows else None
    for row in rows:
        index = day_today(row, zones)
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
        if row.timezone:
            detail.append(f"timezone {row.timezone}")
        if row.preferred_channel:
            detail.append(f"prefers {row.preferred_channel}")
        lines.append(
            ", ".join(detail) + ". "
            + _next_contact_text(row, configs, last)
        )
    return "\n".join(lines)


def _team_lines(settings: Any, names: dict) -> list[str]:
    """Each team member who receives alerts, with what they handle."""
    out = []
    for member in (settings.alert_recipient_user_ids if settings else []):
        perms = followup_store.member_permissions(settings, member)
        if not perms["receives"] & {"urgent", "attention"}:
            continue
        name = names.get(uuid.UUID(str(member))) or "?"
        out.append(name + (f" — handles: {perms['handles']}" if perms["handles"] else ""))
    return out


def _chosen(settings: Any, names: dict, wanted: Any) -> list[uuid.UUID]:
    """The team members the agent named in ``escalate.to``."""
    needles = [fold_label(str(w)) for w in (wanted or []) if str(w).strip()]
    return [
        uid for uid, name in names.items()
        if any(n and (n in fold_label(name) or n == str(uid)) for n in needles)
    ]


async def _inbox(
    sf: Any,
    ctx: Context,
    settings: Any,
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
                # The inbox is a list anyone with access scrolls: the full
                # number stays on the alert sent to the team member.
                title=followup_store.wording(
                    settings, "inbox_attention_title",
                    name=mask_readable_phones(await ctx.person_label(sf, settings)),
                ),
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

#: Severity marks a model may put before its reason, with their variation
#: selector and spacing.
_MARKS = "🔴🟠🟡🟢⚠\ufe0f \t"


def _severity(raw: Any) -> str:
    """red or yellow, whatever the model wrote: an escalation is never
    refused over its colour, and anything unclear is the milder one."""
    return "red" if str(raw or "").strip().lower() in _RED else "yellow"


async def handle_escalate(arguments: dict, **kwargs: Any) -> str:
    # The severity mark is the platform's to add; a model that wrote its
    # own sent the team "🔴 🔴".
    reason = str(arguments.get("reason") or "").strip().lstrip(_MARKS).strip()
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
    escalation_id = uuid.uuid4()
    escalation = followup_store.escalation_key(
        escalation_id, invitation.id if invitation is not None else None,
    )
    inboxed = target_id is not None and await _inbox(
        sf, ctx, settings, service_account_id=target_id,
        event_type="checkin.escalation" if invitation is not None else "followup.escalation",
        reason=reason, payload={**payload, "escalation": escalation},
    )
    team = [uuid.UUID(str(u)) for u in (settings.alert_recipient_user_ids if settings else [])]
    to = _chosen(settings, await followup_store.display_names(sf, team), arguments.get("to"))
    queued, why_not = await _tell_team(
        sf, ctx, settings, kind="alert",
        text=f"🔴 {reason}" if severity == "red" else reason, reason=reason,
        severity=severity, dedupe_prefix=escalation,
        topic="urgent" if severity == "red" else "attention", to=to or None,
    )

    if invitation is not None:
        # One write records what happened, on the check-in's own history.
        if inboxed:
            state, note = "raised", None
        elif queued:
            state = "raised"
            note = (
                "Inbox target is not a valid service account; team notified"
                if target_id is not None else "No inbox target; team notified"
            )
        else:
            # A visible failure, never a silent one: the agent saying it
            # escalated is not evidence that anyone was told.
            state, note = "failed", "No escalation target or team member configured"
        async with sf() as db:
            row = await db.get(ProgramInvitationRow, invitation.id)
            row.escalation_state = state
            if note:
                row.reason = note
            await db.commit()

    # Recorded even with no known person: the team's queue must show it.
    await followup_store.record_escalation(
        sf, escalation_id=escalation_id, org_id=ctx.org_id, agent_id=ctx.agent_id,
        user_id=await ctx.person_user_id(sf), kind="alert", reason=reason, severity=severity,
        state="raised" if inboxed or queued else "failed", platform=ctx.channel or None,
        session_id=ctx.session_id,
        invitation_id=invitation.id if invitation is not None else None,
        program_id=invitation.program_id if invitation is not None else None,
    )

    if not inboxed and not queued:
        return (
            "Could not escalate: nobody is configured to receive this "
            f"({why_not}). Tell the person to contact the business directly."
        )
    told = []
    if inboxed:
        told.append("the responsible operator's inbox")
    if queued:
        told.append(f"{queued} team member(s)")
    return (
        "Escalated to " + " and ".join(told) + "."
        + (f" Not everyone: {why_not}; the team's queue shows who." if queued and why_not else "")
        + " This note is for you, not "
        "the person: do not repeat it to them; tell them only what they need "
        "to know, in your own words."
    )


def _retryable(name: str, handler: Any) -> Any:
    """*handler*, answering an unexpected failure in words the model acts on.

    A raw exception reads as final: a model that saw a request to stop
    fail once concluded the person was still enrolled and never tried
    again, so the person kept getting messages.
    """
    async def run(arguments: dict, **kwargs: Any) -> str:
        try:
            return await handler(arguments, **kwargs)
        except Exception:
            logger.exception("[followup] %s failed", name)
            return (
                f"Not done: {name} failed on the business's side and nothing was "
                "saved. The person's request still stands: call it again now. "
                "If it fails again, call escalate with what the person asked, "
                "so the team does it, and tell the person it will be handled."
            )
    return run


def register(registry: Any) -> None:
    """Register the follow-up and escalation tools."""
    for name, schema, handler in (
        ("followup_enroll", FOLLOWUP_ENROLL_SCHEMA, handle_followup_enroll),
        ("followup_update", FOLLOWUP_UPDATE_SCHEMA, handle_followup_update),
        ("followup_status", FOLLOWUP_STATUS_SCHEMA, handle_followup_status),
        ("escalate", ESCALATE_SCHEMA, handle_escalate),
        ("followup_contacts", FOLLOWUP_CONTACTS_SCHEMA, handle_followup_contacts),
        ("followup_withdraw_consent", FOLLOWUP_WITHDRAW_CONSENT_SCHEMA,
         handle_followup_withdraw_consent),
    ):
        registry.register(
            name=name, schema=schema, handler=_retryable(name, handler), toolset="followup",
        )
