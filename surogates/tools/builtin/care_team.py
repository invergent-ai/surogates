"""Tools for the practice's care team when they write to the agent themselves.

The follow-up tools act only on the person the conversation belongs to, so a
patient's conversation can never reach another patient.  These are the
deliberate exception: they read across every person enrolled with this agent.
That is safe only because they work for nobody but an alert recipient of this
agent, proved from the database on every call, and are withheld from every
other session's tool list.
"""

from __future__ import annotations

import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa

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
from surogates.session.events import EventType
from surogates.tools.builtin.followup import _context
from surogates.tools.registry import ToolSchema

CARE_TEAM_TOOL_NAMES: frozenset[str] = frozenset({"care_team_overview"})

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
                    "their latest conversation lines."
                ),
            },
        },
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


def _fold(text: str) -> str:
    """Case- and accent-insensitive: "stefan" finds "Ștefan"."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


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
                .where(Event.created_at >= since)
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
            head += f" · +{person.phone}"
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


def register(registry: Any) -> None:
    for name, schema, handler in (
        ("care_team_overview", CARE_TEAM_OVERVIEW_SCHEMA, handle_care_team_overview),
    ):
        registry.register(name=name, schema=schema, handler=handler, toolset="care_team")
