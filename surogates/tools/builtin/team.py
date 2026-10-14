"""Tools for the team behind an agent when they write to it themselves.

The follow-up tools act only on the person the conversation belongs to, so a
person's conversation can never reach another's.  These are the
deliberate exception: they read across, and write to, every person enrolled
with this agent.  That is safe only because they work for nobody but an alert
recipient of this agent, proved from the database on every call, and are
withheld from every other session's tool list.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from surogates.channels.capabilities import capabilities_for
from surogates.channels.identity import (
    channel_user_email, mask_readable_phones, masked_phone, normalize_wa_id, readable_phone,
)
from surogates.channels.memory_boundary import session_memory_boundary
from surogates.db.agent_users import SOURCE_MANUAL, ensure_agent_user
from surogates.db.models import (
    ChannelContactRow,
    ChannelIdentity,
    Event,
    OutreachMessageRow,
    ProgramInvitationRow,
    Session as SessionRow,
    User,
)
from surogates.followup import store as followup_store
from surogates.followup.context import (
    channel_names,
    in_person_zone,
    load_context,
    local_time,
    start_date_from,
    start_text,
    told,
)
from surogates.followup.eligibility import as_utc, day_index, next_contact
from surogates.followup.outreach import (
    Described,
    describe_people,
    operator_unanswered_hours,
    plain_reason,
    window_open,
)
from surogates.runtime.memory_io import read_user_memory
from surogates.runtime.memory_protocol import memory_object_key
from surogates.session.events import EventType
from surogates.session.interactive_input import fold_label
from surogates.tools.registry import ToolSchema

logger = logging.getLogger(__name__)

TEAM_TOOL_NAMES: frozenset[str] = frozenset({
    "team_overview", "team_message_person", "team_update_person",
    "team_enroll_person",
})

NOT_TEAM = (
    "Refused: this tool is only for the business's team, and the person "
    "in this conversation is not on it. Nothing about anyone else can be shown "
    "or sent from here; do not mention other people."
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
#: ops' default ``texts.welcome_text``, used when the business set none.
WELCOME_TEXT = (
    "Thank you. To get started, could you tell me your start date?"
)

#: Phone numbers never reach the model whole; the platform sends them.
PHONE_HINT = (
    "numbers are shown masked here; team_overview with include_phone sends "
    "the team member the whole number in a separate message."
)

TEAM_OVERVIEW_SCHEMA = ToolSchema(
    name="team_overview",
    description=(
        "Team only. A digest of everyone enrolled with this business's "
        "follow-up: protocol, day, status, when they last wrote, their last "
        "scheduled step and its outcome, alerts in the last 72 hours, unanswered "
        "messages in a row and the next scheduled message, with a header of "
        "recent enrolments, pauses and people silent for 3 days or more. "
        "Summarise it for the team member using only what it says; never rank or "
        "interpret values."
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
            "person": {
                "type": "string",
                "description": (
                    "A name, phone fragment or channel handle to show only that person, with "
                    "their latest conversation lines and what the agent "
                    "remembers about them."
                ),
            },
            "include_phone": {
                "type": "boolean",
                "description": (
                    "true when the team member asks for a phone number. "
                    "Numbers in this digest are masked; with this, the "
                    "platform sends the team member the whole numbers of the "
                    "people shown in a separate message. Never write a number "
                    "out yourself."
                ),
            },
        },
    },
)

TEAM_MESSAGE_PERSON_SCHEMA = ToolSchema(
    name="team_message_person",
    description=(
        "Team only. Send an enrolled person the team member's message, word "
        "for word, on their channel: the one they prefer, else the one they "
        "last wrote on. First call without "
        "confirmation: it shows who would receive what, sends nothing and "
        "gives a confirmation code. Show that to the team member and only after "
        "they answer yes call again with the same arguments and that code. "
        "Never reword the team member's text."
    ),
    parameters={
        "type": "object",
        "properties": {
            "person": {
                "type": "string",
                "description": "The person's name, a fragment of their phone number, or their handle on a channel.",
            },
            "text": {
                "type": "string",
                "description": "The team member's message exactly as they wrote it.",
            },
            "even_if_paused": {
                "type": "boolean",
                "description": (
                    "Only true after you told the team member the person's "
                    "follow-up is paused or ended and they still want this sent."
                ),
            },
            "confirmation": {
                "type": "string",
                "description": (
                    "The code the preview of this exact action gave, only "
                    "after the team member answered yes to that preview."
                ),
            },
        },
        "required": ["person", "text"],
    },
)

TEAM_UPDATE_PERSON_SCHEMA = ToolSchema(
    name="team_update_person",
    description=(
        "Team only. Change an enrolled person's follow-up on a team "
        "member's word: pause, end or resume it, correct the start date, the "
        "person's name or who answers for them. First call without "
        "confirmation: it shows exactly what would change, changes nothing "
        "and gives a confirmation code. Show that to the team member and only "
        "after they answer yes call again with the same arguments and that "
        "code."
    ),
    parameters={
        "type": "object",
        "properties": {
            "person": {
                "type": "string",
                "description": "The person's name, a fragment of their phone number, or their handle on a channel.",
            },
            "protocol": {
                "type": "string",
                "description": "The protocol slug. Omit when the person has only one.",
            },
            "status": {
                "type": "string",
                "enum": list(followup_store.ENROLLMENT_STATUSES),
                "description": "active to resume, paused or ended.",
            },
            "reason": {
                "type": "string",
                "description": "Why, in the team member's words. Required when pausing or ending.",
            },
            "start_date": {
                "type": "string",
                "description": "The corrected day 0, as YYYY-MM-DD. Give this or days_ago.",
            },
            "days_ago": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "The corrected day 0 as days before today, when the team member "
                    "says it that way. Do not work out the date yourself."
                ),
            },
            "start_date_approximate": {
                "type": "boolean",
                "description": "True when the team member gave the start only roughly.",
            },
            "person_name": {
                "type": "string",
                "description": "The corrected person's full name.",
            },
            "answered_by": {
                "type": "string",
                "description": (
                    "Who now answers the scheduled messages for the person, with their "
                    "relation, e.g. \"Ana, daughter\"."
                ),
            },
            "confirmation": {
                "type": "string",
                "description": (
                    "The code the preview of this exact action gave, only "
                    "after the team member answered yes to that preview."
                ),
            },
        },
        "required": ["person"],
    },
)

TEAM_ENROLL_PERSON_SCHEMA = ToolSchema(
    name="team_enroll_person",
    description=(
        "Team only. Enrol a new person by their WhatsApp number on a team "
        "member's word: it records their permission to be messaged as the "
        "team member's say-so and sends them the business's welcome message. "
        "First call without confirmation: it shows who would be enrolled and "
        "how, changes nothing and gives a confirmation code. Show that to the "
        "team member and only after they answer yes call again with the same "
        "arguments and that code."
    ),
    parameters={
        "type": "object",
        "properties": {
            "phone": {
                "type": "string",
                "description": "The person's WhatsApp number with country code, e.g. +40 746 148 303.",
            },
            "person_name": {"type": "string", "description": "The person's full name."},
            "protocol": {
                "type": "string",
                "description": "The protocol slug. Omit when the business offers only one.",
            },
            "start_date": {
                "type": "string",
                "description": "Day 0 as YYYY-MM-DD. Give this, days_ago or date_unknown.",
            },
            "days_ago": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Day 0 as days before today, when the team member says it that "
                    "way. Do not work out the date yourself."
                ),
            },
            "start_date_approximate": {
                "type": "boolean",
                "description": "True when the team member gave the start only roughly.",
            },
            "date_unknown": {
                "type": "boolean",
                "description": "True only when the team member does not know day 0.",
            },
            "answered_by": {
                "type": "string",
                "description": (
                    "Who answers the scheduled messages when not the person, with their "
                    "relation, e.g. \"Ana, daughter\"."
                ),
            },
            "confirmation": {
                "type": "string",
                "description": (
                    "The code the preview of this exact action gave, only "
                    "after the team member answered yes to that preview."
                ),
            },
        },
        "required": ["phone", "person_name"],
    },
)


async def team_tool_exclusions(
    session_factory: Any, session: Any, *, programs_enabled: bool,
) -> frozenset[str]:
    """The team tools, unless this session's user is on the team."""
    if programs_enabled and getattr(session, "user_id", None) is not None:
        settings = await followup_store.get_settings(
            session_factory, org_id=session.org_id, agent_id=session.agent_id,
        )
        if followup_store.is_team_member(settings, session.user_id):
            return frozenset()
    return TEAM_TOOL_NAMES


# ---------------------------------------------------------------------------
# Who is enrolled
# ---------------------------------------------------------------------------


class _Person:
    def __init__(self, described: Described) -> None:
        self.user_id = described.user_id
        self.rows = described.rows
        self.display_name = described.display_name
        self.phone = described.wa_id
        self.handles = described.handles
        self.label = described.name or f"user {described.user_id}"

    @property
    def contact(self) -> str:
        """How the person is reached, masked: the phone, else a channel id."""
        if self.phone:
            return masked_phone(self.phone)
        for platform, found in self.handles.items():
            if found:
                return f"{platform} …{found[0][-4:]}"
        return "no contact"

    def matches(self, fragment: str) -> bool:
        digits = "".join(c for c in fragment if c.isdigit())
        if len(digits) >= 4 and digits in self.phone:
            return True
        handle = fragment.strip().lstrip("@").casefold()
        if len(handle) >= 3 and any(
            handle in h.casefold()
            for platform, found in self.handles.items() if platform != "whatsapp"
            for h in found
        ):
            return True
        needle = fold_label(fragment.strip())
        names = [self.display_name] + [r.patient_name or "" for r in self.rows]
        return bool(needle) and any(needle in fold_label(n) for n in names)


async def _people(sf: Any, org_id: uuid.UUID, agent_id: str, settings: Any) -> list[_Person]:
    """Everyone enrolled with this agent, never the team itself."""
    by_user: dict[uuid.UUID, list] = {}
    for row in await followup_store.enrollments(sf, org_id=org_id, agent_id=agent_id):
        if not followup_store.is_team_member(settings, row.user_id):
            by_user.setdefault(row.user_id, []).append(row)
    described = await describe_people(sf, org_id=org_id, agent_id=agent_id, rows_by_user=by_user)
    return [_Person(d) for d in described.values()]


def _senders(ctx: Any, settings: Any) -> dict[str, str]:
    """The agent's sender per channel; the team member's own conversation
    names one for its channel when the settings do not."""
    own = {ctx.channel: ctx.sender} if ctx.sender and capabilities_for(ctx.channel).can_initiate else {}
    return {**own, **followup_store.senders_of(settings)}


def _sender(ctx: Any, settings: Any) -> str | None:
    return _senders(ctx, settings).get("whatsapp")


async def _last_wrote(sf: Any, ctx: Any, settings: Any, people: list) -> dict[uuid.UUID, datetime]:
    """When each person last wrote to any of the agent's senders."""
    owner = {
        (platform, handle): p.user_id
        for p in people for platform, found in p.handles.items() for handle in found
    }
    senders = set(_senders(ctx, settings).values())
    if not owner or not senders:
        return {}
    async with sf() as db:
        rows = (
            await db.execute(
                sa.select(
                    ChannelContactRow.platform, ChannelContactRow.platform_user_id,
                    ChannelContactRow.last_inbound_at,
                )
                .where(ChannelContactRow.org_id == ctx.org_id)
                .where(ChannelContactRow.channel_identifier.in_(senders))
                .where(ChannelContactRow.platform_user_id.in_({h for _, h in owner}))
            )
        ).all()
    out: dict[uuid.UUID, datetime] = {}
    for platform, handle, at in rows:
        user_id = owner.get((platform, handle))
        if user_id is not None and (user_id not in out or as_utc(at) > as_utc(out[user_id])):
            out[user_id] = at
    return out


def _ago(at: datetime | None, now: datetime) -> str:
    if at is None:
        return "never"
    seconds = max(0, int((now - as_utc(at)).total_seconds()))
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


async def _one_person(sf: Any, ctx: Any, settings: Any, fragment: str) -> _Person | str:
    """The one enrolled person *fragment* names, or why there is not one."""
    matches = [
        p for p in await _people(sf, ctx.org_id, ctx.agent_id, settings)
        if p.matches(fragment)
    ]
    if not matches:
        return f"nobody enrolled with this business matches '{fragment}'."
    if len(matches) > 1:
        return (
            f"'{fragment}' matches more than one person. Ask the team member which "
            "one:\n" + "\n".join(f"- {p.label} ({p.contact})" for p in matches)
        )
    return matches[0]


def _member(ctx: Any) -> str:
    return ctx.display_name or f"user {ctx.user_id}"


# ---------------------------------------------------------------------------
# team_overview
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

    An escalation is queued once per team phone, keyed
    ``<escalation>:<recipient>``, and recorded as an event carrying the same
    key, so the key, not the wording, says which rows are one escalation;
    its reminder is ``reminded_at`` on it.  Text matches only what has no key.
    Returns ``(at, reason, reminded)`` per person, oldest first.
    """
    since = now - ALERT_WINDOW
    async with sf() as db:
        outreach = (
            await db.execute(
                sa.select(
                    OutreachMessageRow.subject_user_id,
                    OutreachMessageRow.created_at,
                    # Rows queued before the reason was stored have only their text.
                    sa.func.coalesce(OutreachMessageRow.alert_reason, OutreachMessageRow.text),
                    OutreachMessageRow.dedupe_key, OutreachMessageRow.reminded_at,
                )
                .where(OutreachMessageRow.org_id == org_id)
                .where(OutreachMessageRow.agent_id == agent_id)
                .where(OutreachMessageRow.kind == "alert")
                .where(sa.func.coalesce(OutreachMessageRow.created_by, "") != "reminder")
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
    found: dict[uuid.UUID, dict[str, list]] = {}
    reminded: set[tuple[uuid.UUID, str]] = set()
    for user_id, at, reason, dedupe_key, reminded_at in outreach:
        key = dedupe_key.rsplit(":", 1)[0] if dedupe_key else None
        reason = (reason or "").strip()
        if key and reminded_at is not None:
            reminded.add((user_id, key))
        found.setdefault(user_id, {}).setdefault(key or f"text:{reason}", [at, reason])
    for user_id, at, data in escalations:
        reason = str((data or {}).get("reason") or "").strip()
        key = (data or {}).get("escalation") or f"text:{reason}"
        found.setdefault(user_id, {}).setdefault(key, [at, reason])
    out: dict[uuid.UUID, list[tuple]] = {}
    for user_id, items in found.items():
        keyed = {reason for key, (_, reason) in items.items() if not key.startswith("text:")}
        out[user_id] = sorted(
            (
                (at, reason, (user_id, key) in reminded)
                for key, (at, reason) in items.items()
                if reason and not (key.startswith("text:") and reason in keyed)
            ),
            key=lambda x: as_utc(x[0]),
        )
    return out


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
        if inv.reminder_state:
            text += f" ({inv.reminder_state})"
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
            who = "business" if data.get("synthetic") == "outreach" else "agent"
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
        logger.warning("[team] no memory bucket configured", exc_info=True)
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

    async def read(key: str) -> Any:
        try:
            return await read_user_memory(backend, bucket=bucket, key=key)
        except Exception:
            logger.warning("[team] memory read failed for %s", key, exc_info=True)
            return None

    keys = [
        memory_object_key(
            storage_key_prefix=tenant.asset_root, user_id=str(user_id),
            boundary=boundary, target=target,
        )
        for boundary in boundaries for target in ("user", "memory")
    ]
    entries: list[str] = []
    for found in await asyncio.gather(*(read(key) for key in keys)):
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


async def handle_team_overview(arguments: dict, **kwargs: Any) -> str:
    sf = kwargs["session_factory"]
    ctx, settings = await load_context(kwargs)
    if ctx is None or not followup_store.is_team_member(settings, ctx.user_id):
        return NOT_TEAM
    try:
        since_hours = int(arguments.get("since_hours") or 24)
    except (TypeError, ValueError):
        since_hours = 24
    since_hours = min(max(since_hours, 1), 720)
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=since_hours)

    people = await _people(sf, ctx.org_id, ctx.agent_id, settings)
    fragment = str(arguments.get("person") or arguments.get("patient") or "").strip()
    if fragment:
        people = [p for p in people if p.matches(fragment)]
        if not people:
            return f"Nobody enrolled with this business matches '{fragment}'."
    if not people:
        return "Nobody is enrolled with this business's follow-up yet."

    ids = [p.user_id for p in people]

    configs, last_in, invitations, alerts, undelivered = await asyncio.gather(
        followup_store.active_program_configs(sf, org_id=ctx.org_id, agent_id=ctx.agent_id),
        _last_wrote(sf, ctx, settings, people),
        _invitations(sf, ctx.org_id, ctx.agent_id, ids, now),
        _alerts(sf, ctx.org_id, ctx.agent_id, ids, now),
        _undelivered(sf, ctx.org_id, ctx.agent_id, ids, now),
    )
    zones = await followup_store.protocol_timezones(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id, configs=configs,
    )

    new, changed, silent = [], [], []
    for person in people:
        wrote = last_in.get(person.user_id)
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

    async def block(person: _Person) -> str:
        wrote = last_in.get(person.user_id)
        head = person.label
        head += f" · {person.contact}"
        lines = [f"• {head}"]
        for row in person.rows:
            zone = row.timezone or zones.get(row.protocol, "UTC")
            index = day_index(row.start_date, datetime.now(ZoneInfo(zone)).date())
            text = f"  {row.protocol}: " + (f"day {index}" if index is not None else "no start date")
            if row.phase:
                text += f", phase {row.phase}"
            text += f", {row.status}"
            if row.status_reason:
                text += f" ({row.status_reason})"
            if row.status == "active":
                found = next_contact(
                    in_person_zone(configs, row), protocol=row.protocol, start_date=row.start_date,
                    phase=row.phase, enrolled_at=row.created_at,
                    last_inbound_at=wrote, now=now,
                )
                text += "; next message " + (local_time(*found) if found else "none scheduled")
            lines.append(text)
        invs = invitations.get(person.user_id, [])
        lines.append(
            f"  last wrote {_ago(wrote, now)}; last step {_last_checkin(invs)}; "
            f"unanswered messages in a row: {_unanswered_streak(invs)}"
        )
        for at, reason, was_reminded in alerts.get(person.user_id, []):
            lines.append(
                f"  alert {as_utc(at):%m-%d %H:%M}: {reason[:300]}"
                + (" (reminded)" if was_reminded else "")
            )
        if fragment and len(people) == 1:
            convo = await _conversation(sf, ctx.org_id, ctx.agent_id, person.user_id)
            lines.append("  latest conversation:" if convo else "  no conversation yet")
            lines.extend(convo)
            lines.extend(_memory_lines(
                await _memory(kwargs, sf, ctx, person.user_id)
            ))
        return "\n".join(lines)

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
            f"{p.label}: {plain_reason(reason, settings.texts if settings else None)}, call them ({p.contact})"
            for p in people for reason in undelivered.get(p.user_id, [])
        ) or "none"),
    ]
    out = "\n".join(header)
    shown = 0
    # Only as many people as the reply can hold are written out.
    for person in people[:MAX_PEOPLE]:
        text = await block(person)
        if len(out) + len(text) > MAX_CHARS:
            break
        out += "\n\n" + text
        shown += 1
    if shown < len(people):
        out += (
            f"\n\n… {len(people) - shown} more not shown; ask about one person "
            "with the person argument."
        )
    if arguments.get("include_phone") is True:
        out += "\n\n" + await _send_phones(sf, ctx, settings, people[:shown])
    return mask_readable_phones(out)


async def _send_phones(sf: Any, ctx: Any, settings: Any, people: list) -> str:
    """Send the asking team member the whole numbers, outside the model.

    Platform text to their phone: the event the model reads keeps them
    masked, the outbox row carries them whole.
    """
    lines = [f"{p.label}: {readable_phone(p.phone)}" for p in people if p.phone]
    if not lines:
        return "Phone numbers: nobody shown has a number on record."
    queued, why_not = await followup_store.notify_team(
        sf, settings, org_id=ctx.org_id, agent_id=ctx.agent_id, kind="notice",
        text="📞 " + "\n📞 ".join(lines), created_by="team-overview",
        only_user_id=ctx.user_id, fallback_senders={ctx.channel: ctx.sender},
    )
    if not queued:
        return f"Phone numbers: could not be sent ({why_not})."
    return (
        "Phone numbers: sent to the team member in a separate message within "
        "a minute. Tell them so; do not repeat or guess any number."
    )


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------

CONFIRM_TTL_SECONDS = 15 * 60


def _confirm_key(session_id: Any, tool: str, code: str) -> str:
    return f"surogates:team:confirm:{session_id}:{tool}:{code}"


async def _unconfirmed(
    kwargs: dict, tool: str, arguments: dict, preview: str, nothing_done: str,
) -> str | None:
    """None when *arguments* carry the code that a preview of this exact
    action issued in this session; otherwise the preview with a fresh code.

    The preview text is what the team member said yes to, so its hash is the
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
        f"{lead}{preview} Show this to the team member; only after they answer yes, "
        f"call again with the same arguments and confirmation=\"{fresh}\". "
        f"{nothing_done}"
    )


# ---------------------------------------------------------------------------
# team_message_person
# ---------------------------------------------------------------------------


async def handle_team_message_person(arguments: dict, **kwargs: Any) -> str:
    sf = kwargs["session_factory"]
    ctx, settings = await load_context(kwargs)
    if ctx is None or not followup_store.is_team_member(settings, ctx.user_id):
        return NOT_TEAM
    fragment = str(arguments.get("person") or arguments.get("patient") or "").strip()
    text = str(arguments.get("text") or "").strip()
    if not fragment:
        return "Nothing sent: say which person, by name or phone number."
    if not text:
        return "Nothing sent: the message is empty."
    if len(text) > MAX_MESSAGE:
        return f"Nothing sent: keep a message under {MAX_MESSAGE:,} characters."

    person = await _one_person(sf, ctx, settings, fragment)
    if isinstance(person, str):
        return f"Nothing sent: {person}"

    async with sf() as db:
        found = (
            await db.execute(
                sa.select(ChannelIdentity)
                .where(ChannelIdentity.org_id == ctx.org_id)
                .where(ChannelIdentity.user_id == person.user_id)
                .order_by(ChannelIdentity.id.desc())
            )
        ).scalars().all()
    identities: dict[str, Any] = {}
    for identity in found:
        if capabilities_for(identity.platform).can_initiate:
            identities.setdefault(identity.platform, identity)
    if not identities:
        return f"Nothing sent: {person.label} has no number or account the business can write to."
    phone = person.contact
    call = f"Call them instead ({phone}); {PHONE_HINT}"
    active = any(r.status == "active" for r in person.rows)
    if not active and arguments.get("even_if_paused") is not True:
        status = "; ".join(
            f"{r.protocol} {r.status}" + (f" ({r.status_reason})" if r.status_reason else "")
            for r in person.rows
        )
        return (
            f"Nothing sent: {person.label}'s follow-up is not active ({status}). "
            "Ask the team member whether to message them anyway; only if they say "
            f"yes, call again with even_if_paused: true. {call}"
        )
    granted = {
        p: i for p, i in identities.items() if followup_store.permission_status(i) == "granted"
    }
    if not granted:
        return (
            f"Nothing sent: {person.label} has not given permission to be "
            f"messaged on {channel_names(identities)}. {call}"
        )
    senders = _senders(ctx, settings)
    if not any(p in senders for p in granted):
        return f"Nothing sent: {channel_names(granted)} is not switched on for this business. {call}"
    now = datetime.now(timezone.utc)
    last: dict[str, datetime | None] = {}
    for p in granted:
        if p in senders:
            last[p] = await followup_store.last_inbound(
                sf, org_id=ctx.org_id, platform=p,
                platform_user_id=granted[p].platform_user_id, channel_identifier=senders[p],
            )
    # A bot cannot write first to someone who never wrote to it.
    usable = [p for p in last if last[p] is not None or not capabilities_for(p).needs_prior_contact]
    if not usable:
        return f"Nothing sent: {person.label} has never written to the business on {channel_names(last)}. {call}"
    preferred = next(
        (r.preferred_channel for r in person.rows if r.status == "active" and r.preferred_channel in usable),
        None,
    )
    wrote = [p for p in usable if last[p] is not None]
    platform = preferred or (
        max(wrote, key=lambda p: as_utc(last[p])) if wrote
        else "whatsapp" if "whatsapp" in usable else usable[0]
    )
    name = channel_names([platform])
    windowed = bool(capabilities_for(platform).window_hours)
    template = (settings.message_template_name if settings else None) if windowed else None
    is_open = not windowed or window_open(last[platform], now)
    if not is_open and not template:
        return (
            f"Nothing sent: {person.label} has not written in the last 24 hours, "
            f"so {name} only lets an approved message template reach them, and "
            f"the business has none chosen under Follow-up. {call}"
        )

    refused = await _unconfirmed(
        kwargs, "team_message_person", arguments,
        f"Will send to {person.label} ({phone}): «{text}»"
        + (
            f", now, as a normal {name} message." if is_open else
            ". Their 24h window is closed, so the approved opener template goes "
            "first and your text follows the moment they answer."
        ),
        "Nothing has been sent.",
    )
    if refused:
        return refused

    await followup_store.queue_outreach(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id, user_id=person.user_id,
        platform=platform, channel_identifier=senders[platform], kind="operator",
        text=text, template_name=template,
        template_language=(settings.message_template_language if settings and template else None),
        subject_user_id=person.user_id, created_by=f"team:{ctx.user_id}",
    )
    if is_open:
        return (
            f"Sent now: your text goes out to {person.label} as a normal "
            f"{name} message within a minute."
        )
    hours = operator_unanswered_hours(settings)
    return (
        f"{person.label}'s 24h window is closed: I sent the opener template; "
        "your text goes the moment they answer. You will be told here when it "
        f"arrives, or if they have not answered within {hours} hours."
    )




# ---------------------------------------------------------------------------
# team_update_person
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


async def handle_team_update_person(arguments: dict, **kwargs: Any) -> str:
    sf = kwargs["session_factory"]
    ctx, settings = await load_context(kwargs)
    if ctx is None or not followup_store.is_team_member(settings, ctx.user_id):
        return NOT_TEAM
    fragment = str(arguments.get("person") or arguments.get("patient") or "").strip()
    if not fragment:
        return "Nothing changed: say which person, by name or phone number."
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
        start = start_date_from(arguments, zones.get(row.protocol, "UTC"))
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
    given = {
        "patient_name": arguments.get("person_name") or arguments.get("patient_name"),
        "answered_by": arguments.get("answered_by"),
    }
    for field, label in (("patient_name", "name"), ("answered_by", "answered by")):
        value = str(given[field] or "").strip()
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
            return "Nothing changed: ask the team member why before pausing or ending."
        changes["status"] = status
        # No column records who changed an enrolment; the reason carries it.
        changes["status_reason"] = f"{reason} (by {_member(ctx)})" if reason else None
        described.append(f"status {row.status} → {status}" + (f" ({reason})" if reason else ""))
    if not changes:
        return "Nothing to change: say what to change."

    refused = await _unconfirmed(
        kwargs, "team_update_person", arguments,
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
        result += f", person_name {updated.patient_name}"
    if updated.answered_by:
        result += f", answered by {updated.answered_by}"
    if updated.start_date:
        result += f", start {start_text(updated)}"
    result += "."
    if status in ("paused", "ended"):
        label = followup_store.person_label(person.display_name, updated)
        queued, why_not = await followup_store.notify_team(
            sf, settings, org_id=ctx.org_id, agent_id=ctx.agent_id, kind="alert",
            text=(
                f"{row.protocol} {status} by {_member(ctx)}: {reason}"
                f"\n\n— {label or person.label}"
            ),
            reason=f"{row.protocol} {status} by {_member(ctx)}: {reason}",
            created_by=f"team:{ctx.user_id}", subject_user_id=person.user_id,
            exclude_user_id=ctx.user_id, fallback_senders={ctx.channel: ctx.sender},
        )
        result += " " + told(queued, why_not)
    return result


# ---------------------------------------------------------------------------
# team_enroll_person
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


async def _person_account(
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
            return f"that person's account already has the number {masked_phone(bound)}."
        # Someone who wrote first has a runtime shadow account, which the
        # linked identity policy refuses; the team member vouching makes them known.
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


async def handle_team_enroll_person(arguments: dict, **kwargs: Any) -> str:
    sf = kwargs["session_factory"]
    ctx, settings = await load_context(kwargs)
    if ctx is None or not followup_store.is_team_member(settings, ctx.user_id):
        return NOT_TEAM
    try:
        wa_id = normalize_wa_id(str(arguments.get("phone") or ""))
    except ValueError as exc:
        return f"Not enrolled: {exc}"
    name = str(arguments.get("person_name") or arguments.get("patient_name") or "").strip()
    if not name:
        return "Not enrolled: ask the team member for the person's full name."
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
        start = start_date_from(arguments, zones[protocol])
    except ValueError as exc:
        return f"Not enrolled: {exc}."
    if start is None and arguments.get("date_unknown") is not True:
        return (
            "Not enrolled: ask the team member for day 0 and pass days_ago or "
            "start_date, or date_unknown: true if they do not know it."
        )
    approximate = arguments.get("start_date_approximate") is True if start else None
    answered_by = str(arguments.get("answered_by") or "").strip() or None

    holder = await _number_holder(sf, ctx.org_id, wa_id)
    if holder is not None and followup_store.is_team_member(settings, holder.id):
        return (
            f"Not enrolled: {masked_phone(wa_id)} belongs to a member of the team, "
            "who receives the alerts and is not followed. Check the number."
        )
    sender = _sender(ctx, settings)

    when = (
        f"from {start}" + (" (approximate)" if approximate else "")
        if start else "with no start date, so no Program reaches them until one is set"
    )
    refused = await _unconfirmed(
        kwargs, "team_enroll_person", arguments,
        f"Will enrol {name} ({masked_phone(wa_id)}) in {protocol} {when}"
        + (f", answered by {answered_by}" if answered_by else "")
        + (f"; the number is already known as {holder.display_name}" if holder else "")
        + "; record their permission to be messaged on your say-so"
        + ("; and send them the business's welcome message" if sender else "")
        + ".",
        "Nothing has been changed.",
    )
    if refused:
        return refused

    user_id = await _person_account(sf, ctx, wa_id, name, holder)
    if isinstance(user_id, str):
        return f"Not enrolled: {user_id}"
    await followup_store.grant_contact_permission(
        sf, org_id=ctx.org_id, platform="whatsapp", platform_user_id=wa_id,
        recorded_by=f"team:{ctx.user_id}", method="team",
    )
    try:
        row = await followup_store.upsert_enrollment(
            sf, org_id=ctx.org_id, agent_id=ctx.agent_id, user_id=user_id,
            protocol=protocol, start_date=start, phase=None, enrolled_by="team",
            source_ref=f"team:{ctx.user_id}", patient_name=name,
            answered_by=answered_by, start_date_approximate=approximate,
        )
    except followup_store.TeamMember:
        return f"Not enrolled: {masked_phone(wa_id)} belongs to a member of the team."
    result = f"Enrolled {name} ({masked_phone(wa_id)}) in {protocol}"
    if row.start_date:
        result += f" from {start_text(row)}"
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
            subject_user_id=user_id, created_by=f"team:{ctx.user_id}",
            dedupe_key=(
                f"welcome:team:{ctx.agent_id}:{user_id}:{protocol}:"
                f"{datetime.now(timezone.utc):%Y-%m-%d}"
            ),
        )
        result += f" Welcome message queued (state {welcome.state})."
    else:
        result += " No welcome sent: WhatsApp is not switched on for this business."
    queued, why_not = await followup_store.notify_team(
        sf, settings, org_id=ctx.org_id, agent_id=ctx.agent_id, kind="enrolment",
        text=(
            f"{name} enrolled in {protocol} by {_member(ctx)}."
            f"\n\n— {followup_store.person_label(name, row)} · {readable_phone(wa_id)}"
        ),
        created_by=f"team:{ctx.user_id}", subject_user_id=user_id,
        exclude_user_id=ctx.user_id, fallback_senders={ctx.channel: ctx.sender},
    )
    return result + " " + told(queued, why_not)


def register(registry: Any) -> None:
    for name, schema, handler in (
        ("team_overview", TEAM_OVERVIEW_SCHEMA, handle_team_overview),
        ("team_message_person", TEAM_MESSAGE_PERSON_SCHEMA,
         handle_team_message_person),
        ("team_update_person", TEAM_UPDATE_PERSON_SCHEMA,
         handle_team_update_person),
        ("team_enroll_person", TEAM_ENROLL_PERSON_SCHEMA,
         handle_team_enroll_person),
    ):
        registry.register(name=name, schema=schema, handler=handler, toolset="team")
