"""Tools for the practice's care team when they write to the agent themselves.

The follow-up tools act only on the person the conversation belongs to, so a
patient's conversation can never reach another patient.  These two are the
deliberate exception: they read across, and write to, every person enrolled
with this agent.  That is safe only because they work for nobody but an alert
recipient of this agent, proved from the database on every call, and are
withheld from every other session's tool list.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from surogates.channels.identity import channel_user_email, normalize_wa_id, readable_phone
from surogates.channels.memory_boundary import session_memory_boundary
from surogates.db.agent_users import SOURCE_MANUAL, ensure_agent_user
from surogates.db.models import (
    ChannelIdentity,
    Event,
    OutreachMessageRow,
    ProgramInvitationRow,
    Session as SessionRow,
    User,
)
from surogates.followup import store as followup_store
from surogates.followup.eligibility import as_utc, day_index, next_contact
from surogates.followup.outreach import operator_unanswered_hours, plain_reason, window_open
from surogates.runtime.memory_io import read_user_memory
from surogates.runtime.memory_protocol import memory_object_key
from surogates.session.events import EventType
from surogates.session.interactive_input import _fold
from surogates.tools.builtin.followup import _context, _start_date, _start_text, _told
from surogates.tools.registry import ToolSchema

logger = logging.getLogger(__name__)

CARE_TEAM_TOOL_NAMES: frozenset[str] = frozenset({
    "care_team_overview", "care_team_message_patient", "care_team_update_patient",
    "care_team_enroll_patient",
})

NOT_CARE_TEAM = (
    "Refused: this tool is only for the practice's care team, and the person "
    "in this conversation is not on it. Nothing about anyone else can be shown "
    "or sent from here; do not mention other patients."
)

ALERT_WINDOW = timedelta(hours=72)
SILENT_AFTER = timedelta(days=3)
#: Invitations older than this do not change the digest and are not read.
HISTORY = timedelta(days=60)
MAX_PEOPLE = 60
MAX_CHARS = 12_000
CONVERSATION_LINES = 10
MEMORY_CHARS = 3000
MAX_MESSAGE = 4000
#: ops' default ``texts.welcome_text``, used when the practice set none.
WELCOME_TEXT = (
    "Thank you. To start your follow-up, could you tell me the date of your procedure?"
)

CARE_TEAM_OVERVIEW_SCHEMA = ToolSchema(
    name="care_team_overview",
    description=(
        "Care team only. A digest of everyone enrolled with this practice's "
        "follow-up: protocol, day, status, when they last wrote, their last "
        "check-in and its outcome, alerts in the last 72 hours, unanswered "
        "check-ins in a row and the next scheduled message, with a header of "
        "recent enrolments, pauses and people silent for 3 days or more. "
        "Summarise it for the doctor using only what it says; never rank or "
        "interpret clinical values."
    ),
    parameters={
        "type": "object",
        "properties": {
            "since_hours": {
                "type": "integer",
                "minimum": 1,
                "maximum": 720,
                "description": "How far back the header looks. Default 24.",
            },
            "patient": {
                "type": "string",
                "description": (
                    "A name or phone fragment to show only that person, with "
                    "their latest conversation lines and what the agent "
                    "remembers about them."
                ),
            },
        },
    },
)

CARE_TEAM_MESSAGE_PATIENT_SCHEMA = ToolSchema(
    name="care_team_message_patient",
    description=(
        "Care team only. Send an enrolled patient the doctor's message, word "
        "for word, from the practice's WhatsApp number. First call without "
        "confirmation: it shows who would receive what, sends nothing and "
        "gives a confirmation code. Show that to the doctor and only after "
        "they answer yes call again with the same arguments and that code. "
        "Never reword the doctor's text."
    ),
    parameters={
        "type": "object",
        "properties": {
            "patient": {
                "type": "string",
                "description": "The patient's name or a fragment of their phone number.",
            },
            "text": {
                "type": "string",
                "description": "The doctor's message exactly as they wrote it.",
            },
            "even_if_paused": {
                "type": "boolean",
                "description": (
                    "Only true after you told the doctor the patient's "
                    "follow-up is paused or ended and they still want this sent."
                ),
            },
            "confirmation": {
                "type": "string",
                "description": (
                    "The code the preview of this exact action gave, only "
                    "after the doctor answered yes to that preview."
                ),
            },
        },
        "required": ["patient", "text"],
    },
)

CARE_TEAM_UPDATE_PATIENT_SCHEMA = ToolSchema(
    name="care_team_update_patient",
    description=(
        "Care team only. Change an enrolled patient's follow-up on the "
        "doctor's word: pause, end or resume it, correct the start date, the "
        "patient's name or who answers for them. First call without "
        "confirmation: it shows exactly what would change, changes nothing "
        "and gives a confirmation code. Show that to the doctor and only "
        "after they answer yes call again with the same arguments and that "
        "code."
    ),
    parameters={
        "type": "object",
        "properties": {
            "patient": {
                "type": "string",
                "description": "The patient's name or a fragment of their phone number.",
            },
            "protocol": {
                "type": "string",
                "description": "The protocol slug. Omit when the patient has only one.",
            },
            "status": {
                "type": "string",
                "enum": list(followup_store.ENROLLMENT_STATUSES),
                "description": "active to resume, paused or ended.",
            },
            "reason": {
                "type": "string",
                "description": "Why, in the doctor's words. Required when pausing or ending.",
            },
            "start_date": {
                "type": "string",
                "description": "The corrected day 0, as YYYY-MM-DD. Give this or days_ago.",
            },
            "days_ago": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "The corrected day 0 as days before today, when the doctor "
                    "says it that way. Do not work out the date yourself."
                ),
            },
            "start_date_approximate": {
                "type": "boolean",
                "description": "True when the doctor gave the start only roughly.",
            },
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
            "confirmation": {
                "type": "string",
                "description": (
                    "The code the preview of this exact action gave, only "
                    "after the doctor answered yes to that preview."
                ),
            },
        },
        "required": ["patient"],
    },
)

CARE_TEAM_ENROLL_PATIENT_SCHEMA = ToolSchema(
    name="care_team_enroll_patient",
    description=(
        "Care team only. Enrol a new patient by their WhatsApp number on the "
        "doctor's word: it records their permission to be messaged as the "
        "doctor's say-so and sends them the practice's welcome message. First "
        "call without confirmation: it shows who would be enrolled and how, "
        "changes nothing and gives a confirmation code. Show that to the "
        "doctor and only after they answer yes call again with the same "
        "arguments and that code."
    ),
    parameters={
        "type": "object",
        "properties": {
            "phone": {
                "type": "string",
                "description": "The patient's WhatsApp number with country code, e.g. +40 746 148 303.",
            },
            "patient_name": {"type": "string", "description": "The patient's full name."},
            "protocol": {
                "type": "string",
                "description": "The protocol slug. Omit when the practice offers only one.",
            },
            "start_date": {
                "type": "string",
                "description": "Day 0 as YYYY-MM-DD. Give this, days_ago or date_unknown.",
            },
            "days_ago": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Day 0 as days before today, when the doctor says it that "
                    "way. Do not work out the date yourself."
                ),
            },
            "start_date_approximate": {
                "type": "boolean",
                "description": "True when the doctor gave the start only roughly.",
            },
            "date_unknown": {
                "type": "boolean",
                "description": "True only when the doctor does not know day 0.",
            },
            "answered_by": {
                "type": "string",
                "description": (
                    "Who answers the check-ins when not the patient, with their "
                    "relation, e.g. \"Ana, daughter\"."
                ),
            },
            "confirmation": {
                "type": "string",
                "description": (
                    "The code the preview of this exact action gave, only "
                    "after the doctor answered yes to that preview."
                ),
            },
        },
        "required": ["phone", "patient_name"],
    },
)


async def care_team_tool_exclusions(
    session_factory: Any, session: Any, *, programs_enabled: bool,
) -> frozenset[str]:
    """The care-team tools, unless this session's user is on the care team."""
    if programs_enabled and getattr(session, "user_id", None) is not None:
        settings = await followup_store.get_settings(
            session_factory, org_id=session.org_id, agent_id=session.agent_id,
        )
        if followup_store.is_care_team(settings, session.user_id):
            return frozenset()
    return CARE_TEAM_TOOL_NAMES


# ---------------------------------------------------------------------------
# Who is enrolled
# ---------------------------------------------------------------------------


class _Person:
    def __init__(self, user_id: uuid.UUID, rows: list, user: Any, phone: str) -> None:
        self.user_id = user_id
        self.rows = rows
        self.display_name = (user.display_name if user else "") or ""
        self.phone = phone
        self.label = followup_store.patient_label(
            self.display_name, followup_store.labelling_enrollment(rows),
        ) or f"user {user_id}"

    def matches(self, fragment: str) -> bool:
        digits = "".join(c for c in fragment if c.isdigit())
        if len(digits) >= 4 and digits in self.phone:
            return True
        needle = _fold(fragment.strip())
        names = [self.display_name] + [r.patient_name or "" for r in self.rows]
        return bool(needle) and any(needle in _fold(n) for n in names)


async def _people(sf: Any, org_id: uuid.UUID, agent_id: str, settings: Any) -> list[_Person]:
    """Everyone enrolled with this agent, never the care team itself."""
    by_user: dict[uuid.UUID, list] = {}
    for row in await followup_store.enrollments(sf, org_id=org_id, agent_id=agent_id):
        if not followup_store.is_care_team(settings, row.user_id):
            by_user.setdefault(row.user_id, []).append(row)
    if not by_user:
        return []
    ids = list(by_user)
    async with sf() as db:
        users = {
            u.id: u for u in (
                await db.execute(sa.select(User).where(User.id.in_(ids)))
            ).scalars()
        }
        phones = dict(
            (
                await db.execute(
                    sa.select(ChannelIdentity.user_id, ChannelIdentity.platform_user_id)
                    .where(ChannelIdentity.org_id == org_id)
                    .where(ChannelIdentity.platform == "whatsapp")
                    .where(ChannelIdentity.user_id.in_(ids))
                )
            ).all()
        )
    return [
        _Person(uid, rows, users.get(uid), phones.get(uid) or "")
        for uid, rows in by_user.items()
    ]


def _sender(ctx: Any, settings: Any) -> str | None:
    return (settings.channel_identifier if settings else None) or (
        ctx.sender if ctx.channel == "whatsapp" else None
    ) or None


def _ago(at: datetime | None, now: datetime) -> str:
    if at is None:
        return "never"
    seconds = max(0, int((now - as_utc(at)).total_seconds()))
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _local(at: datetime, config: dict) -> str:
    zone = str(config.get("timezone") or "UTC")
    return f"{at.astimezone(ZoneInfo(zone)):%a %Y-%m-%d %H:%M} {zone}"


async def _one_person(sf: Any, ctx: Any, settings: Any, fragment: str) -> _Person | str:
    """The one enrolled person *fragment* names, or why there is not one."""
    matches = [
        p for p in await _people(sf, ctx.org_id, ctx.agent_id, settings)
        if p.matches(fragment)
    ]
    if not matches:
        return f"nobody enrolled with this practice matches '{fragment}'."
    if len(matches) > 1:
        return (
            f"'{fragment}' matches more than one person. Ask the doctor which "
            "one:\n" + "\n".join(f"- {p.label} ({readable_phone(p.phone) if p.phone else 'no number'})" for p in matches)
        )
    return matches[0]


def _doctor(ctx: Any) -> str:
    return ctx.display_name or f"user {ctx.user_id}"


# ---------------------------------------------------------------------------
# care_team_overview
# ---------------------------------------------------------------------------


async def _invitations(sf, org_id, agent_id, ids, now) -> dict[uuid.UUID, list]:
    async with sf() as db:
        rows = (
            await db.execute(
                sa.select(ProgramInvitationRow)
                .where(ProgramInvitationRow.org_id == org_id)
                .where(ProgramInvitationRow.agent_id == agent_id)
                .where(ProgramInvitationRow.user_id.in_(ids))
                .where(ProgramInvitationRow.created_at >= now - HISTORY)
                .order_by(ProgramInvitationRow.created_at.desc())
            )
        ).scalars().all()
    out: dict[uuid.UUID, list] = {}
    for row in rows:
        out.setdefault(row.user_id, []).append(row)
    return out


async def _alerts(sf, org_id, agent_id, ids, now) -> dict[uuid.UUID, list[tuple]]:
    """Each person's alerts and escalations of the last 72 hours, once each.

    An alert is queued once per care-team phone, and an escalation is also an
    event on the person's session, so the same reason is read several times.
    """
    since = now - ALERT_WINDOW
    async with sf() as db:
        outreach = (
            await db.execute(
                sa.select(
                    OutreachMessageRow.subject_user_id,
                    OutreachMessageRow.created_at, OutreachMessageRow.text,
                )
                .where(OutreachMessageRow.org_id == org_id)
                .where(OutreachMessageRow.agent_id == agent_id)
                .where(OutreachMessageRow.kind == "alert")
                .where(OutreachMessageRow.subject_user_id.in_(ids))
                .where(OutreachMessageRow.created_at >= since)
            )
        ).all()
        escalations = (
            await db.execute(
                sa.select(SessionRow.user_id, Event.created_at, Event.data)
                .join(SessionRow, SessionRow.id == Event.session_id)
                .where(SessionRow.org_id == org_id)
                .where(SessionRow.agent_id == agent_id)
                .where(SessionRow.user_id.in_(ids))
                .where(Event.type.in_(("followup.escalation", "checkin.escalation")))
                # events.created_at is naive UTC; asyncpg refuses an aware bound.
                .where(Event.created_at >= since.replace(tzinfo=None))
            )
        ).all()
    out: dict[uuid.UUID, dict[str, datetime]] = {}
    for user_id, at, text in outreach:
        reason = (text or "").split("\n\n— ")[0].strip()
        out.setdefault(user_id, {}).setdefault(reason, at)
    for user_id, at, data in escalations:
        reason = str((data or {}).get("reason") or "").strip()
        out.setdefault(user_id, {}).setdefault(reason, at)
    return {
        uid: sorted(((at, r) for r, at in reasons.items() if r), key=lambda x: as_utc(x[0]))
        for uid, reasons in out.items()
    }


async def _undelivered(sf, org_id, agent_id, ids, now) -> dict[uuid.UUID, list[str]]:
    """Why each person's recent messages failed to reach them, once per reason."""
    async with sf() as db:
        rows = (
            await db.execute(
                sa.select(OutreachMessageRow.user_id, OutreachMessageRow.reason)
                .where(OutreachMessageRow.org_id == org_id)
                .where(OutreachMessageRow.agent_id == agent_id)
                .where(OutreachMessageRow.kind.in_(("operator", "welcome")))
                .where(OutreachMessageRow.state == "failed")
                .where(OutreachMessageRow.user_id.in_(ids))
                .where(OutreachMessageRow.created_at >= now - ALERT_WINDOW)
                .order_by(OutreachMessageRow.created_at)
            )
        ).all()
    out: dict[uuid.UUID, list[str]] = {}
    for user_id, reason in rows:
        out.setdefault(user_id, [])
        if reason not in out[user_id]:
            out[user_id].append(reason)
    return out


def _unanswered_streak(invitations: list) -> int:
    count = 0
    for inv in invitations:
        if inv.delivery_state in ("skipped", "canceled"):
            continue
        if inv.response_state != "no_reply_by_deadline":
            break
        count += 1
    return count


def _last_checkin(invitations: list) -> str:
    for inv in invitations:
        if inv.delivery_state in ("skipped", "canceled"):
            continue
        text = (
            f"{as_utc(inv.created_at):%Y-%m-%d}: delivery {inv.delivery_state}, "
            f"response {inv.response_state}"
        )
        if inv.escalation_state != "none":
            text += f", escalation {inv.escalation_state}"
        if inv.reason:
            text += f" — {inv.reason[:200]}"
        return text
    return "none"


async def _conversation(sf, org_id, agent_id, user_id) -> list[str]:
    """The person's latest messages and the agent's replies, oldest first."""
    async with sf() as db:
        rows = (
            await db.execute(
                sa.select(Event.type, Event.data, Event.created_at)
                .join(SessionRow, SessionRow.id == Event.session_id)
                .where(SessionRow.org_id == org_id)
                .where(SessionRow.agent_id == agent_id)
                .where(SessionRow.user_id == user_id)
                .where(Event.type.in_((EventType.USER_MESSAGE, EventType.LLM_RESPONSE)))
                .order_by(Event.id.desc())
                .limit(CONVERSATION_LINES * 4)
            )
        ).all()
    lines: list[str] = []
    for kind, data, at in rows:
        data = data or {}
        if kind == EventType.USER_MESSAGE:
            if data.get("synthetic"):
                continue  # the platform's instruction to the agent, not the person
            who, content = "them", data.get("content")
        else:
            who = "practice" if data.get("synthetic") == "outreach" else "agent"
            content = (data.get("message") or {}).get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        lines.append(f"  [{as_utc(at):%m-%d %H:%M}] {who}: {content.strip()[:300]}")
        if len(lines) == CONVERSATION_LINES:
            break
    return lines[::-1]


def _memory_bucket() -> str:
    from surogates.config import load_settings

    storage = load_settings().storage
    return storage.memory_bucket or storage.bucket


async def _memory(kwargs: dict, sf: Any, ctx: Any, user_id: uuid.UUID) -> list[str] | None:
    """What the agent remembers about one person, from every partition the
    worker keys their memory by: each of their one-to-one channel
    conversations' boundary, then their per-user memory."""
    backend, tenant = kwargs.get("storage"), kwargs.get("tenant")
    if backend is None or tenant is None:
        return None
    try:
        bucket = _memory_bucket()
    except Exception:
        logger.warning("[care_team] no memory bucket configured", exc_info=True)
        return None
    async with sf() as db:
        sessions = (
            await db.execute(
                sa.select(SessionRow)
                .where(SessionRow.org_id == ctx.org_id)
                .where(SessionRow.agent_id == ctx.agent_id)
                .where(SessionRow.user_id == user_id)
                .order_by(SessionRow.created_at.desc())
            )
        ).scalars().all()
    boundaries: dict[str | None, None] = {}
    for session in sessions:
        boundary = session_memory_boundary(session)
        # A group's or Slack's shared partition describes a room, not them.
        if boundary and boundary != "public" and not (session.config or {}).get("multi_party"):
            boundaries[boundary] = None
    boundaries[None] = None
    entries: list[str] = []
    for boundary in boundaries:
        for target in ("user", "memory"):
            key = memory_object_key(
                storage_key_prefix=tenant.asset_root, user_id=str(user_id),
                boundary=boundary, target=target,
            )
            try:
                found = await read_user_memory(backend, bucket=bucket, key=key)
            except Exception:
                logger.warning("[care_team] memory read failed for %s", key, exc_info=True)
                continue
            if found:
                entries.extend(e.strip() for e in found[0].split("\n---\n") if e.strip())
    return list(dict.fromkeys(entries))


def _memory_lines(entries: list[str] | None) -> list[str]:
    if entries is None:
        return ["  the agent's memory cannot be read here"]
    if not entries:
        return ["  the agent remembers nothing about them yet"]
    lines, used = ["  what the agent remembers about them:"], 0
    for entry in entries:
        line = f"  - {' '.join(entry.split())[:600]}"
        if used + len(line) > MEMORY_CHARS:
            lines.append("  - … more not shown")
            break
        lines.append(line)
        used += len(line)
    return lines


async def handle_care_team_overview(arguments: dict, **kwargs: Any) -> str:
    sf = kwargs["session_factory"]
    ctx, settings = await _context(kwargs)
    if ctx is None or not followup_store.is_care_team(settings, ctx.user_id):
        return NOT_CARE_TEAM
    try:
        since_hours = int(arguments.get("since_hours") or 24)
    except (TypeError, ValueError):
        since_hours = 24
    since_hours = min(max(since_hours, 1), 720)
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=since_hours)

    people = await _people(sf, ctx.org_id, ctx.agent_id, settings)
    fragment = str(arguments.get("patient") or "").strip()
    if fragment:
        people = [p for p in people if p.matches(fragment)]
        if not people:
            return f"Nobody enrolled with this practice matches '{fragment}'."
    if not people:
        return "Nobody is enrolled with this practice's follow-up yet."

    ids = [p.user_id for p in people]
    configs = await followup_store.active_program_configs(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id,
    )
    zones = await followup_store.protocol_timezones(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id, configs=configs,
    )
    sender = _sender(ctx, settings)
    last_in = await followup_store.last_inbound_by_user(
        sf, org_id=ctx.org_id, platform="whatsapp", channel_identifier=sender or "",
        platform_user_ids=[p.phone for p in people if p.phone],
    ) if sender else {}
    invitations = await _invitations(sf, ctx.org_id, ctx.agent_id, ids, now)
    alerts = await _alerts(sf, ctx.org_id, ctx.agent_id, ids, now)
    undelivered = await _undelivered(sf, ctx.org_id, ctx.agent_id, ids, now)

    new, changed, silent = [], [], []
    blocks: list[str] = []
    for person in people:
        wrote = last_in.get(person.phone)
        active = [r for r in person.rows if r.status == "active"]
        for row in person.rows:
            if as_utc(row.created_at) >= since:
                new.append(f"{person.label} ({row.protocol})")
            if row.status != "active" and as_utc(row.updated_at) >= since:
                changed.append(
                    f"{person.label}: {row.protocol} {row.status}"
                    + (f" — {row.status_reason}" if row.status_reason else "")
                )
        quiet_since = wrote or (min(as_utc(r.created_at) for r in active) if active else None)
        if quiet_since is not None and now - as_utc(quiet_since) >= SILENT_AFTER:
            silent.append(f"{person.label} (wrote {_ago(wrote, now)})")

        head = person.label
        if person.phone:
            head += f" · {readable_phone(person.phone)}"
        lines = [f"• {head}"]
        for row in person.rows:
            zone = zones.get(row.protocol, "UTC")
            index = day_index(row.start_date, datetime.now(ZoneInfo(zone)).date())
            text = f"  {row.protocol}: " + (f"day {index}" if index is not None else "no start date")
            if row.phase:
                text += f", phase {row.phase}"
            text += f", {row.status}"
            if row.status_reason:
                text += f" ({row.status_reason})"
            if row.status == "active":
                found = next_contact(
                    configs, protocol=row.protocol, start_date=row.start_date,
                    phase=row.phase, enrolled_at=row.created_at,
                    last_inbound_at=wrote, now=now,
                )
                text += "; next message " + (_local(*found) if found else "none scheduled")
            lines.append(text)
        invs = invitations.get(person.user_id, [])
        lines.append(
            f"  last wrote {_ago(wrote, now)}; last check-in {_last_checkin(invs)}; "
            f"unanswered check-ins in a row: {_unanswered_streak(invs)}"
        )
        for at, reason in alerts.get(person.user_id, []):
            lines.append(f"  alert {as_utc(at):%m-%d %H:%M}: {reason[:300]}")
        if fragment and len(people) == 1:
            convo = await _conversation(sf, ctx.org_id, ctx.agent_id, person.user_id)
            lines.append("  latest conversation:" if convo else "  no conversation yet")
            lines.extend(convo)
            lines.extend(_memory_lines(
                await _memory(kwargs, sf, ctx, person.user_id)
            ))
        blocks.append("\n".join(lines))

    header = [
        (
            f"Follow-up overview at {now:%Y-%m-%d %H:%M} UTC, {len(people)} "
            f"{'person' if len(people) == 1 else 'people'}. Raw records: quote, "
            "do not interpret."
        ),
        f"New enrolments (last {since_hours}h): " + ("; ".join(new) or "none"),
        f"Paused or ended (last {since_hours}h): " + ("; ".join(changed) or "none"),
        "Silent 3 days or more: " + ("; ".join(silent) or "none"),
        "Open items, messages that did not arrive (last 72h): " + ("; ".join(
            f"{p.label}: {plain_reason(reason)}, call {readable_phone(p.phone) if p.phone else 'no number'}"
            for p in people for reason in undelivered.get(p.user_id, [])
        ) or "none"),
    ]
    out = "\n".join(header)
    shown = 0
    for block in blocks[:MAX_PEOPLE]:
        if len(out) + len(block) > MAX_CHARS:
            break
        out += "\n\n" + block
        shown += 1
    if shown < len(blocks):
        out += (
            f"\n\n… {len(blocks) - shown} more not shown; ask about one person "
            "with the patient argument."
        )
    return out


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------

CONFIRM_TTL_SECONDS = 15 * 60


def _confirm_key(session_id: Any, tool: str, code: str) -> str:
    return f"surogates:care_team:confirm:{session_id}:{tool}:{code}"


async def _unconfirmed(
    kwargs: dict, tool: str, arguments: dict, preview: str, nothing_done: str,
) -> str | None:
    """None when *arguments* carry the code that a preview of this exact
    action issued in this session; otherwise the preview with a fresh code.

    The preview text is what the doctor said yes to, so its hash is the
    binding: any change to who, what or the record's current state since
    the preview makes the old code worthless.
    """
    redis = kwargs.get("redis")
    session_id = kwargs["session_id"]
    digest = hashlib.sha256(f"{tool}\n{preview}".encode()).hexdigest()
    code = str(arguments.get("confirmation") or "").strip().upper()
    lead = ""
    if code and redis is not None:
        held = await redis.getdel(_confirm_key(session_id, tool, code))
        if isinstance(held, bytes):
            held = held.decode()
        if held == digest:
            return None
        lead = (
            "Not done: that confirmation code has expired or was given for a "
            "different action. "
        )
    elif code:
        lead = "Not done: confirmations are unavailable right now. "
    elif arguments.get("confirmed") is True:
        lead = "Not done: a yes needs the confirmation code from the preview. "
    if redis is None:
        return f"{lead}{preview} {nothing_done}"
    fresh = secrets.token_hex(3).upper()
    await redis.set(_confirm_key(session_id, tool, fresh), digest, ex=CONFIRM_TTL_SECONDS)
    return (
        f"{lead}{preview} Show this to the doctor; only after they answer yes, "
        f"call again with the same arguments and confirmation=\"{fresh}\". "
        f"{nothing_done}"
    )


# ---------------------------------------------------------------------------
# care_team_message_patient
# ---------------------------------------------------------------------------


async def handle_care_team_message_patient(arguments: dict, **kwargs: Any) -> str:
    sf = kwargs["session_factory"]
    ctx, settings = await _context(kwargs)
    if ctx is None or not followup_store.is_care_team(settings, ctx.user_id):
        return NOT_CARE_TEAM
    fragment = str(arguments.get("patient") or "").strip()
    text = str(arguments.get("text") or "").strip()
    if not fragment:
        return "Nothing sent: say which patient, by name or phone number."
    if not text:
        return "Nothing sent: the message is empty."
    if len(text) > MAX_MESSAGE:
        return f"Nothing sent: keep a message under {MAX_MESSAGE:,} characters."

    person = await _one_person(sf, ctx, settings, fragment)
    if isinstance(person, str):
        return f"Nothing sent: {person}"

    async with sf() as db:
        identity = (
            await db.execute(
                sa.select(ChannelIdentity)
                .where(ChannelIdentity.org_id == ctx.org_id)
                .where(ChannelIdentity.platform == "whatsapp")
                .where(ChannelIdentity.user_id == person.user_id)
            )
        ).scalars().first()
    if identity is None:
        return f"Nothing sent: {person.label} has no WhatsApp number."
    phone = readable_phone(identity.platform_user_id)
    call = f"Call them instead: {phone}."
    active = any(r.status == "active" for r in person.rows)
    if not active and arguments.get("even_if_paused") is not True:
        status = "; ".join(
            f"{r.protocol} {r.status}" + (f" ({r.status_reason})" if r.status_reason else "")
            for r in person.rows
        )
        return (
            f"Nothing sent: {person.label}'s follow-up is not active ({status}). "
            "Ask the doctor whether to message them anyway; only if they say "
            f"yes, call again with even_if_paused: true. {call}"
        )
    if followup_store.permission_status(identity) != "granted":
        return (
            f"Nothing sent: {person.label} has not given permission to be "
            f"messaged on WhatsApp. {call}"
        )
    sender = _sender(ctx, settings)
    if not sender:
        return f"Nothing sent: WhatsApp is not switched on for this practice. {call}"
    template = settings.message_template_name if settings else None
    is_open = window_open(
        await followup_store.last_inbound(
            sf, org_id=ctx.org_id, platform="whatsapp",
            platform_user_id=identity.platform_user_id, channel_identifier=sender,
        ),
        datetime.now(timezone.utc),
    )
    if not is_open and not template:
        return (
            f"Nothing sent: {person.label} has not written in the last 24 hours, "
            "so WhatsApp only lets an approved message template reach them, and "
            f"the practice has none chosen under Follow-up. {call}"
        )

    refused = await _unconfirmed(
        kwargs, "care_team_message_patient", arguments,
        f"Will send to {person.label} ({phone}): «{text}»"
        + (
            ", now, as a normal WhatsApp message." if is_open else
            ". Their 24h window is closed, so the approved opener template goes "
            "first and your text follows the moment they answer."
        ),
        "Nothing has been sent.",
    )
    if refused:
        return refused

    await followup_store.queue_outreach(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id, user_id=person.user_id,
        platform="whatsapp", channel_identifier=sender, kind="operator",
        text=text, template_name=template,
        template_language=settings.message_template_language if settings else None,
        subject_user_id=person.user_id, created_by=f"care-team:{ctx.user_id}",
    )
    if is_open:
        return (
            f"Sent now: {person.label} wrote in the last 24 hours, so your text "
            "goes out as a normal WhatsApp message within a minute."
        )
    hours = operator_unanswered_hours(settings)
    return (
        f"{person.label}'s 24h window is closed: I sent the opener template; "
        "your text goes the moment they answer. You will be told here when it "
        f"arrives, or if they have not answered within {hours} hours."
    )


# ---------------------------------------------------------------------------
# care_team_update_patient
# ---------------------------------------------------------------------------


def _enrollment_of(person: _Person, protocol: str) -> Any:
    if protocol:
        row = next((r for r in person.rows if r.protocol == protocol), None)
        return row or (
            f"{person.label} is not enrolled in '{protocol}'; enrolled in "
            + ", ".join(r.protocol for r in person.rows) + "."
        )
    if len(person.rows) == 1:
        return person.rows[0]
    return (
        f"{person.label} is enrolled in several protocols ("
        + ", ".join(r.protocol for r in person.rows) + "); say which."
    )


async def handle_care_team_update_patient(arguments: dict, **kwargs: Any) -> str:
    sf = kwargs["session_factory"]
    ctx, settings = await _context(kwargs)
    if ctx is None or not followup_store.is_care_team(settings, ctx.user_id):
        return NOT_CARE_TEAM
    fragment = str(arguments.get("patient") or "").strip()
    if not fragment:
        return "Nothing changed: say which patient, by name or phone number."
    person = await _one_person(sf, ctx, settings, fragment)
    if isinstance(person, str):
        return f"Nothing changed: {person}"
    row = _enrollment_of(person, str(arguments.get("protocol") or "").strip())
    if isinstance(row, str):
        return f"Nothing changed: {row}"

    zones = await followup_store.protocol_timezones(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id,
    )
    changes: dict[str, Any] = {}
    described: list[str] = []
    try:
        start = _start_date(arguments, zones.get(row.protocol, "UTC"))
    except ValueError as exc:
        return f"Nothing changed: {exc}."
    approximate = arguments.get("start_date_approximate")
    if start is not None:
        changes["start_date"] = start
        described.append(
            f"start date {row.start_date or 'none'} → {start}"
            + (" (approximate)" if approximate is True else "")
        )
    if isinstance(approximate, bool):
        changes["start_date_approximate"] = approximate
        if start is None:
            described.append(f"start date marked {'approximate' if approximate else 'exact'}")
    for field, label in (("patient_name", "patient name"), ("answered_by", "answered by")):
        value = str(arguments.get(field) or "").strip()
        if value:
            changes[field] = value
            described.append(f"{label} {getattr(row, field) or 'none'} → {value}")
    status = arguments.get("status")
    reason = str(arguments.get("reason") or "").strip()
    if status is not None:
        if status not in followup_store.ENROLLMENT_STATUSES:
            return (
                "Nothing changed: status must be one of "
                f"{', '.join(followup_store.ENROLLMENT_STATUSES)}."
            )
        if status in ("paused", "ended") and not reason:
            return "Nothing changed: ask the doctor why before pausing or ending."
        changes["status"] = status
        # No column records who changed an enrolment; the reason carries it.
        changes["status_reason"] = f"{reason} (by {_doctor(ctx)})" if reason else None
        described.append(f"status {row.status} → {status}" + (f" ({reason})" if reason else ""))
    if not changes:
        return "Nothing to change: say what to change."

    refused = await _unconfirmed(
        kwargs, "care_team_update_patient", arguments,
        f"Will change {person.label}'s {row.protocol}: " + "; ".join(described) + ".",
        "Nothing has been changed.",
    )
    if refused:
        return refused

    updated = await followup_store.update_enrollment(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id, user_id=person.user_id,
        protocol=row.protocol, **changes,
    )
    if updated is None:
        return f"Nothing changed: {person.label} is no longer enrolled in {row.protocol}."
    result = f"Updated {person.label}'s {row.protocol}: status {updated.status}"
    if updated.patient_name:
        result += f", patient {updated.patient_name}"
    if updated.answered_by:
        result += f", answered by {updated.answered_by}"
    if updated.start_date:
        result += f", start {_start_text(updated)}"
    result += "."
    if status in ("paused", "ended"):
        label = followup_store.patient_label(person.display_name, updated)
        queued, why_not = await followup_store.notify_care_team(
            sf, settings, org_id=ctx.org_id, agent_id=ctx.agent_id, kind="alert",
            text=(
                f"{row.protocol} {status} by {_doctor(ctx)}: {reason}"
                f"\n\n— {label or person.label}"
            ),
            created_by=f"care-team:{ctx.user_id}", subject_user_id=person.user_id,
            exclude_user_id=ctx.user_id, fallback_sender=_sender(ctx, settings),
        )
        result += " " + _told(queued, why_not)
    return result


# ---------------------------------------------------------------------------
# care_team_enroll_patient
# ---------------------------------------------------------------------------


async def _number_holder(sf: Any, org_id: uuid.UUID, wa_id: str) -> User | None:
    """The account this number already belongs to, the way ops resolves it:
    the identity holding the number, else the phone-only account's e-mail."""
    async with sf() as db:
        held = (
            await db.execute(
                sa.select(ChannelIdentity.user_id)
                .where(ChannelIdentity.org_id == org_id)
                .where(ChannelIdentity.platform == "whatsapp")
                .where(ChannelIdentity.platform_user_id == wa_id)
            )
        ).scalar_one_or_none()
        if held is not None:
            return await db.get(User, held)
        return (
            await db.execute(
                sa.select(User)
                .where(User.org_id == org_id)
                .where(sa.func.lower(User.email) == channel_user_email("whatsapp", wa_id, org_id))
                .order_by(User.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()


async def _patient_account(
    sf: Any, ctx: Any, wa_id: str, name: str, holder: User | None,
) -> uuid.UUID | str:
    """The account behind *wa_id*, created phone-only when there is none, in
    the shape ops' Users page creates: synthetic e-mail, external auth, the
    number bound, a runtime shadow promoted, and bound to this agent."""
    async with sf() as db:
        user = await db.get(User, holder.id) if holder is not None else None
        if user is None:
            user = User(
                org_id=ctx.org_id, display_name=name, auth_provider="external",
                email=channel_user_email("whatsapp", wa_id, ctx.org_id),
            )
            db.add(user)
            await db.flush()
        bound = (
            await db.execute(
                sa.select(ChannelIdentity.platform_user_id)
                .where(ChannelIdentity.org_id == ctx.org_id)
                .where(ChannelIdentity.platform == "whatsapp")
                .where(ChannelIdentity.user_id == user.id)
            )
        ).scalar_one_or_none()
        if bound is None:
            db.add(ChannelIdentity(
                org_id=ctx.org_id, user_id=user.id, platform="whatsapp",
                platform_user_id=wa_id, platform_meta={},
            ))
        elif bound != wa_id:
            # Never move a person's number, as the Users page refuses to.
            return f"that person's account already has the number {readable_phone(bound)}."
        # Someone who wrote first has a runtime shadow account, which the
        # linked identity policy refuses; the doctor vouching makes them known.
        if user.auth_provider == "whatsapp":
            user.auth_provider = "external"
        await ensure_agent_user(
            db, org_id=ctx.org_id, agent_id=ctx.agent_id, user_id=user.id,
            source=SOURCE_MANUAL, reactivate=True,
        )
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return "the number was registered for someone else at the same moment; try again."
        return user.id


async def handle_care_team_enroll_patient(arguments: dict, **kwargs: Any) -> str:
    sf = kwargs["session_factory"]
    ctx, settings = await _context(kwargs)
    if ctx is None or not followup_store.is_care_team(settings, ctx.user_id):
        return NOT_CARE_TEAM
    try:
        wa_id = normalize_wa_id(str(arguments.get("phone") or ""))
    except ValueError as exc:
        return f"Not enrolled: {exc}"
    name = str(arguments.get("patient_name") or "").strip()
    if not name:
        return "Not enrolled: ask the doctor for the patient's full name."
    zones = await followup_store.protocol_timezones(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id,
    )
    protocol = str(arguments.get("protocol") or "").strip()
    if not protocol and len(zones) == 1:
        [protocol] = zones
    if protocol not in zones:
        return (
            "Not enrolled: " + (
                f"'{protocol}' is not offered here. " if protocol else "say which protocol. "
            ) + "Offered: " + (", ".join(sorted(zones)) or "none (no active Program)") + "."
        )
    try:
        start = _start_date(arguments, zones[protocol])
    except ValueError as exc:
        return f"Not enrolled: {exc}."
    if start is None and arguments.get("date_unknown") is not True:
        return (
            "Not enrolled: ask the doctor for day 0 and pass days_ago or "
            "start_date, or date_unknown: true if they do not know it."
        )
    approximate = arguments.get("start_date_approximate") is True if start else None
    answered_by = str(arguments.get("answered_by") or "").strip() or None

    holder = await _number_holder(sf, ctx.org_id, wa_id)
    if holder is not None and followup_store.is_care_team(settings, holder.id):
        return (
            f"Not enrolled: {readable_phone(wa_id)} belongs to a member of the care team, "
            "who receives the alerts and is not a patient. Check the number."
        )
    sender = _sender(ctx, settings)

    when = (
        f"from {start}" + (" (approximate)" if approximate else "")
        if start else "with no start date, so no Program reaches them until one is set"
    )
    refused = await _unconfirmed(
        kwargs, "care_team_enroll_patient", arguments,
        f"Will enrol {name} ({readable_phone(wa_id)}) in {protocol} {when}"
        + (f", answered by {answered_by}" if answered_by else "")
        + (f"; the number is already known as {holder.display_name}" if holder else "")
        + "; record their permission to be messaged on your say-so"
        + ("; and send them the practice's welcome message" if sender else "")
        + ".",
        "Nothing has been changed.",
    )
    if refused:
        return refused

    user_id = await _patient_account(sf, ctx, wa_id, name, holder)
    if isinstance(user_id, str):
        return f"Not enrolled: {user_id}"
    await followup_store.grant_contact_permission(
        sf, org_id=ctx.org_id, platform="whatsapp", platform_user_id=wa_id,
        recorded_by=f"care-team:{ctx.user_id}", method="care_team",
    )
    try:
        row = await followup_store.upsert_enrollment(
            sf, org_id=ctx.org_id, agent_id=ctx.agent_id, user_id=user_id,
            protocol=protocol, start_date=start, phase=None, enrolled_by="care_team",
            source_ref=f"care-team:{ctx.user_id}", patient_name=name,
            answered_by=answered_by, start_date_approximate=approximate,
        )
    except followup_store.CareTeamMember:
        return f"Not enrolled: {readable_phone(wa_id)} belongs to a member of the care team."
    result = f"Enrolled {name} ({readable_phone(wa_id)}) in {protocol}"
    if row.start_date:
        result += f" from {_start_text(row)}"
    result += ". Permission to message them recorded on your say-so."
    if sender:
        welcome = await followup_store.queue_outreach(
            sf, org_id=ctx.org_id, agent_id=ctx.agent_id, user_id=user_id,
            platform="whatsapp", channel_identifier=sender, kind="welcome",
            text=(
                ((settings.texts or {}).get("welcome_text") or "").strip()
                or WELCOME_TEXT
            ),
            template_name=settings.welcome_template_name,
            template_language=settings.welcome_template_language,
            subject_user_id=user_id, created_by=f"care-team:{ctx.user_id}",
            dedupe_key=(
                f"welcome:care-team:{ctx.agent_id}:{user_id}:{protocol}:"
                f"{datetime.now(timezone.utc):%Y-%m-%d}"
            ),
        )
        result += f" Welcome message queued (state {welcome.state})."
    else:
        result += " No welcome sent: WhatsApp is not switched on for this practice."
    queued, why_not = await followup_store.notify_care_team(
        sf, settings, org_id=ctx.org_id, agent_id=ctx.agent_id, kind="enrolment",
        text=(
            f"{name} enrolled in {protocol} by {_doctor(ctx)}."
            f"\n\n— {followup_store.patient_label(name, row)} · {readable_phone(wa_id)}"
        ),
        created_by=f"care-team:{ctx.user_id}", subject_user_id=user_id,
        exclude_user_id=ctx.user_id, fallback_sender=sender,
    )
    return result + " " + _told(queued, why_not)


def register(registry: Any) -> None:
    for name, schema, handler in (
        ("care_team_overview", CARE_TEAM_OVERVIEW_SCHEMA, handle_care_team_overview),
        ("care_team_message_patient", CARE_TEAM_MESSAGE_PATIENT_SCHEMA,
         handle_care_team_message_patient),
        ("care_team_update_patient", CARE_TEAM_UPDATE_PATIENT_SCHEMA,
         handle_care_team_update_patient),
        ("care_team_enroll_patient", CARE_TEAM_ENROLL_PATIENT_SCHEMA,
         handle_care_team_enroll_patient),
    ):
        registry.register(name=name, schema=schema, handler=handler, toolset="care_team")
