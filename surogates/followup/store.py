"""Reads and writes for the follow-up tables.

Every function takes a session factory, so the runtime and ops (which imports
this package against the same database) share one implementation.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any, Iterable, NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from surogates.channels.capabilities import capabilities_for, initiating_platforms
from surogates.db.models import (
    ChannelContactRow,
    ChannelIdentity,
    EscalationRow,
    FollowupContactRow,
    FollowupEnrollmentRow,
    FollowupSettingsRow,
    InboxItem,
    OutreachMessageRow,
    ProgramScheduleRow,
    Session as SessionRow,
    User,
)
from surogates.followup.eligibility import Audience
from surogates.followup.wording import wording

logger = logging.getLogger(__name__)

__all__ = [
    "ABILITIES",
    "RECEIVE_KINDS",
    "Recipient",
    "Speaker",
    "can",
    "contacts",
    "link_contact",
    "member_permissions",
    "recipients_for",
    "remove_contact",
    "shared_memory_boundary",
    "speaker_for",
    "writer_line",
    "CareTeamMember",
    "TeamMember",
    "ENROLLMENT_SOURCES",
    "ENROLLMENT_STATUSES",
    "OUTREACH_KINDS",
    "SETTINGS_FIELDS",
    "ALERT_SEVERITIES",
    "active_program_configs",
    "claim_escalation",
    "enrollments",
    "get_settings",
    "grant_contact_permission",
    "is_care_team",
    "is_team_member",
    "labelling_enrollment",
    "last_inbound",
    "last_inbound_by_person",
    "last_inbound_by_user",
    "notify_care_team",
    "notify_team",
    "patient_label",
    "person_label",
    "permission_status",
    "protocol_timezones",
    "queue_outreach",
    "record_escalation",
    "record_inbound",
    "render_text",
    "resolve_escalation",
    "senders_of",
    "update_enrollment",
    "update_settings",
    "upsert_enrollment",
]

ENROLLMENT_STATUSES = ("active", "paused", "ended")
ENROLLMENT_SOURCES = ("agent", "operator", "purchase", "team", "api")
OUTREACH_KINDS = ("alert", "operator", "enrolment", "welcome", "notice")

#: Settings fields :func:`update_settings` accepts.
SETTINGS_FIELDS = frozenset({
    "escalation_service_account_id", "channel_identifier",
    "alert_recipient_user_ids", "senders", "team_channels", "team_permissions",
    "team_destinations", "alert_template_name",
    "alert_template_language", "message_template_name",
    "message_template_language", "welcome_template_name",
    "welcome_template_language", "offer_protocols", "texts",
    "operator_unanswered_hours", "red_alert_reminder_minutes", "web_push",
})
ALERT_SEVERITIES = ("red", "yellow")

#: What a team member can be sent, and what they can do, in the team tools
#: and in Studio.  A member's ``team_permissions`` entry lists either set, or
#: says ``"all"``; a member with no entry gets everything.
RECEIVE_KINDS = (
    "urgent", "attention", "missed_replies", "new_enrolments", "daily_report",
    "delivery_failures",
)
ABILITIES = (
    "see_people", "message_person", "enroll", "pause_resume_end", "edit_details",
    "see_full_phones", "manage_team", "handle_escalations",
)
#: What a shared destination receives when it does not say.
DESTINATION_RECEIVES = ("urgent", "attention", "missed_replies")
#: The recipient of a message sent to a shared destination, not a person.
NO_PERSON = uuid.UUID(int=0)
#: Allowed ranges, inclusive, for the settings that time outreach.  The
#: passes that act on them look back far enough to cover the maximum.
SETTINGS_RANGES = {
    "operator_unanswered_hours": (1, 72),
    "red_alert_reminder_minutes": (5, 720),
}


class TeamMember(ValueError):
    """The person is an alert recipient of this agent, not someone followed."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def render_text(custom: str | None, default: str, **values: Any) -> str:
    """The business's wording with *values* filled in, else the default.

    A stray brace or an unknown placeholder in the business's text must not
    cost the message: it falls back to the platform's own sentence.
    """
    text = (custom or "").strip() or default
    try:
        return text.format(**values)
    except (KeyError, IndexError, ValueError):
        logger.warning("[followup] unusable business wording; using the default")
        return default.format(**values)


# ---------------------------------------------------------------------------
# Enrollments
# ---------------------------------------------------------------------------


async def enrollments(
    session_factory: Any,
    *,
    org_id: uuid.UUID,
    agent_id: str,
    user_id: uuid.UUID | None = None,
    protocol: str | None = None,
    status: str | None = None,
    enrolled_by: str | None = None,
    payer_user_id: uuid.UUID | None = None,
) -> list[FollowupEnrollmentRow]:
    """An agent's enrollments, narrowed by whichever filters are given.

    Ordered oldest first, which is the order a roster and a history read.
    """
    query = (
        sa.select(FollowupEnrollmentRow)
        .where(FollowupEnrollmentRow.org_id == org_id)
        .where(FollowupEnrollmentRow.agent_id == agent_id)
        .order_by(FollowupEnrollmentRow.created_at)
    )
    for column, value in (
        (FollowupEnrollmentRow.user_id, user_id),
        (FollowupEnrollmentRow.protocol, protocol),
        (FollowupEnrollmentRow.status, status),
        (FollowupEnrollmentRow.enrolled_by, enrolled_by),
        (FollowupEnrollmentRow.payer_user_id, payer_user_id),
    ):
        if value is not None:
            query = query.where(column == value)
    async with session_factory() as db:
        return list((await db.execute(query)).scalars().all())


def _one_enrollment(org_id, agent_id, user_id, protocol):
    return (
        sa.select(FollowupEnrollmentRow)
        .where(FollowupEnrollmentRow.org_id == org_id)
        .where(FollowupEnrollmentRow.agent_id == agent_id)
        .where(FollowupEnrollmentRow.user_id == user_id)
        .where(FollowupEnrollmentRow.protocol == protocol)
    )


async def upsert_enrollment(
    session_factory: Any,
    *,
    org_id: uuid.UUID,
    agent_id: str,
    user_id: uuid.UUID,
    protocol: str,
    start_date: date | None,
    phase: str | None,
    enrolled_by: str,
    payer_user_id: uuid.UUID | None = None,
    source_ref: str | None = None,
    patient_name: str | None = None,
    answered_by: str | None = None,
    start_date_approximate: bool | None = None,
    timezone: str | None = None,
    preferred_channel: str | None = None,
) -> FollowupEnrollmentRow:
    """Enrol a person, or refresh and reactivate their existing enrollment.

    Enrolling again is how a person comes back after an ended programme, so
    an existing row is reactivated rather than refused.  A ``None`` start
    date or phase leaves what is recorded untouched: re-enrolling someone
    without restating the date must not erase it; the same goes for the
    person's name and who answers.

    Raises :class:`TeamMember` for an alert recipient of this agent —
    whichever path tries it: the agent in conversation, Studio, or a checkout
    that was given a team member's own number.
    """
    if enrolled_by not in ENROLLMENT_SOURCES:
        raise ValueError(f"enrolled_by must be one of {', '.join(ENROLLMENT_SOURCES)}")
    settings = await get_settings(session_factory, org_id=org_id, agent_id=agent_id)
    if is_team_member(settings, user_id):
        raise TeamMember("this person is on the team, not someone followed")
    protocol = protocol.strip()
    timezone = _zone(timezone)
    preferred_channel = _channel(preferred_channel)
    patient_name = (patient_name or "").strip() or None
    answered_by = (answered_by or "").strip() or None
    if start_date_approximate is None and start_date is not None:
        start_date_approximate = False
    for attempt in (1, 2):
        async with session_factory() as db:
            row = (
                await db.execute(_one_enrollment(org_id, agent_id, user_id, protocol))
            ).scalar_one_or_none()
            if row is None:
                row = FollowupEnrollmentRow(
                    org_id=org_id, agent_id=agent_id, user_id=user_id,
                    protocol=protocol, start_date=start_date, phase=phase,
                    status="active", enrolled_by=enrolled_by,
                    payer_user_id=payer_user_id, source_ref=source_ref,
                    patient_name=patient_name, answered_by=answered_by,
                    start_date_approximate=bool(start_date_approximate),
                    timezone=timezone, preferred_channel=preferred_channel,
                )
                db.add(row)
            else:
                if start_date is not None:
                    row.start_date = start_date
                if phase is not None:
                    row.phase = phase
                if payer_user_id is not None:
                    row.payer_user_id = payer_user_id
                if source_ref is not None:
                    row.source_ref = source_ref
                if patient_name is not None:
                    row.patient_name = patient_name
                if answered_by is not None:
                    row.answered_by = answered_by
                if start_date_approximate is not None:
                    row.start_date_approximate = start_date_approximate
                if timezone is not None:
                    row.timezone = timezone
                if preferred_channel is not None:
                    row.preferred_channel = preferred_channel
                row.status = "active"
                row.status_reason = None
                row.updated_at = _utcnow()
            try:
                await db.commit()
            except IntegrityError:
                # Two enrolments for the same person raced; the second
                # attempt finds the winner's row and updates it.
                await db.rollback()
                if attempt == 2:
                    raise
                continue
            await db.refresh(row)
            return row
    raise AssertionError("unreachable")


_UNSET: Any = object()


def _zone(value: str | None) -> str | None:
    """A zone this server's tzdata knows, or ValueError; blank is none."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone {value!r}") from exc
    return value


def _channel(value: str | None) -> str | None:
    value = (value or "").strip()
    if value:
        _known_platforms([value])
    return value or None


async def update_enrollment(
    session_factory: Any,
    *,
    org_id: uuid.UUID,
    agent_id: str,
    user_id: uuid.UUID,
    protocol: str,
    start_date: Any = _UNSET,
    phase: Any = _UNSET,
    status: Any = _UNSET,
    status_reason: Any = _UNSET,
    patient_name: Any = _UNSET,
    answered_by: Any = _UNSET,
    start_date_approximate: Any = _UNSET,
    timezone: Any = _UNSET,
    preferred_channel: Any = _UNSET,
) -> FollowupEnrollmentRow | None:
    """Change an enrollment's fields; only the arguments passed are touched.

    ``None`` is a real value here (clear the phase, forget the start date),
    which is why "not passed" has its own sentinel.  Returns ``None`` when
    the person is not enrolled in that protocol.
    """
    if status is not _UNSET and status not in ENROLLMENT_STATUSES:
        raise ValueError(f"status must be one of {', '.join(ENROLLMENT_STATUSES)}")
    if timezone is not _UNSET:
        timezone = _zone(timezone)
    if preferred_channel is not _UNSET:
        preferred_channel = _channel(preferred_channel)
    async with session_factory() as db:
        row = (
            await db.execute(_one_enrollment(org_id, agent_id, user_id, protocol))
        ).scalar_one_or_none()
        if row is None:
            return None
        if timezone is not _UNSET:
            row.timezone = timezone
        if preferred_channel is not _UNSET:
            row.preferred_channel = preferred_channel
        if start_date is not _UNSET:
            row.start_date = start_date
            # A restated date is exact unless the caller says otherwise.
            if start_date_approximate is _UNSET:
                row.start_date_approximate = False
        if start_date_approximate is not _UNSET:
            row.start_date_approximate = bool(start_date_approximate)
        if patient_name is not _UNSET:
            row.patient_name = (patient_name or "").strip() or None
        if answered_by is not _UNSET:
            row.answered_by = (answered_by or "").strip() or None
        if phase is not _UNSET:
            row.phase = phase or None
        if status is not _UNSET:
            row.status = status
            if status == "active" and status_reason is _UNSET:
                row.status_reason = None
        if status_reason is not _UNSET:
            row.status_reason = status_reason or None
        row.updated_at = _utcnow()
        await db.commit()
        await db.refresh(row)
        return row


#: Every sentence the platform itself writes to the team or to a person
#: outside the agent's own replies, keyed so ``followup_settings.texts`` words
#: it in the business's language.  Placeholders are named in each default.
TEAM_TEXTS = {
    "label_answered_by": "via {answered_by}",
    "label_reported_by": "reported by {contact} ({relation})",
    "label_contact": "contact",
    "label_footer": "About: {label}",
    "inbox_attention_title": "Needs your attention: {name}",
    "label_contact_for": "{contact} ({relation}), for {name}",
    "notice_paused": "{journey} paused: {reason}",
    "notice_ended": "{journey} ended: {reason}",
    "notice_paused_by": "{journey} paused by {member}: {reason}",
    "notice_ended_by": "{journey} ended by {member}: {reason}",
    "notice_resumed_by": "{journey} resumed by {member}: {reason}",
    "notice_enrolled_by": "{name} enrolled in {journey} by {member}.",
    "notice_stopped": "{who} asked to stop messages on {channel}: {what}. {reason}",
    "stop_ended": "{journeys} ended",
    "stop_contact": "they no longer get the follow-up messages there",
    "link_claim_question": (
        "{writer_name} ({writer}) says they are {relation} of {name} and asks to "
        "receive {name}'s messages too. Is {writer} really their number? Please "
        "answer yes or no."
    ),
    "link_request_team": (
        "Link request: {writer_name} ({writer}) says they are {relation} of {name}. "
        "Confirm or refuse it."
    ),
}


def signed(text: str, label: str, texts: Any = None) -> str:
    """*text* to the team, with who it is about underneath."""
    return f"{text.strip()}\n\n{wording(texts, 'label_footer', label=label)}"


def person_label(display_name: str | None, enrollment: Any = None, texts: Any = None) -> str:
    """Who an enrollment is about, as the team knows them.

    The enrollment's person name wins over *display_name*: that is the
    WhatsApp profile of whoever is writing, often a relative.  *texts* words
    the "via" part in the business's language.
    """
    name = (getattr(enrollment, "patient_name", None) or display_name or "").strip()
    parts = [name] if name else []
    answered_by = getattr(enrollment, "answered_by", None)
    if answered_by:
        parts.append(wording(texts, "label_answered_by", answered_by=answered_by))
    return " · ".join(parts)


async def covered_by_business(
    session_factory: Any, *, org_id: uuid.UUID, agent_id: str, user_id: uuid.UUID,
) -> bool:
    """Whether the business brought *user_id* in, so no plan or package may
    stand between them and the agent: on its team, ever followed in one of
    its journeys (active, paused or ended: someone whose follow-up ended can
    still write about an emergency), or a confirmed contact of someone who
    ever was."""
    settings = await get_settings(session_factory, org_id=org_id, agent_id=agent_id)
    if is_team_member(settings, user_id):
        return True
    if await enrollments(session_factory, org_id=org_id, agent_id=agent_id, user_id=user_id):
        return True
    for link in await contacts(session_factory, org_id=org_id, agent_id=agent_id, contact_user_id=user_id):
        if await enrollments(session_factory, org_id=org_id, agent_id=agent_id, user_id=link.person_user_id):
            return True
    return False


def preferred_channel(rows: Iterable[Any]) -> str | None:
    """The channel a person most recently asked for, across all their
    enrollments: the latest-updated one that names a channel."""
    named = [r for r in rows if getattr(r, "preferred_channel", None)]
    if not named:
        return None
    return max(named, key=lambda r: (r.updated_at is not None, r.updated_at or 0)).preferred_channel


def labelling_enrollment(rows: Iterable[FollowupEnrollmentRow]) -> FollowupEnrollmentRow | None:
    """The enrollment that best names a person: active, then one with a name."""
    return min(
        rows, default=None,
        key=lambda r: (r.status != "active", r.patient_name is None),
    )


async def active_program_configs(
    session_factory: Any, *, org_id: uuid.UUID, agent_id: str,
) -> list[dict]:
    """The projected config of each of this agent's active Programs."""
    async with session_factory() as db:
        configs = (
            await db.execute(
                sa.select(ProgramScheduleRow.config)
                .where(ProgramScheduleRow.org_id == org_id)
                .where(ProgramScheduleRow.agent_id == agent_id)
                .where(ProgramScheduleRow.active.is_(True))
                .order_by(ProgramScheduleRow.id)
            )
        ).scalars().all()
    return [config or {} for config in configs]


async def protocol_timezones(
    session_factory: Any,
    *,
    org_id: uuid.UUID,
    agent_id: str,
    configs: list[dict] | None = None,
) -> dict[str, str]:
    """Each protocol some active Program of this agent speaks to, with the
    timezone of the first such Program — the zone a person's day is counted in.

    These are the only protocols a person can be enrolled in: an enrollment
    no Program targets would never produce a single message.
    """
    if configs is None:
        configs = await active_program_configs(
            session_factory, org_id=org_id, agent_id=agent_id,
        )
    zones: dict[str, str] = {}
    for config in configs:
        try:
            audience = Audience.from_config((config or {}).get("audience"))
        except ValueError:
            continue
        if audience is not None:
            zones.setdefault(audience.protocol, str((config or {}).get("timezone") or "UTC"))
    return zones


# ---------------------------------------------------------------------------
# Contact permission
# ---------------------------------------------------------------------------


def permission_status(identity: Any) -> str:
    """``granted``, ``withdrawn``, … or ``none``: may this person be messaged first."""
    meta = getattr(identity, "platform_meta", None) or {}
    return (meta.get("contact_permission") or {}).get("status") or "none"


async def grant_contact_permission(
    session_factory: Any,
    *,
    org_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
    recorded_by: str,
    method: str = "consent",
    status: str = "granted",
) -> bool:
    """Record that the person consented to be messaged (or, with
    ``status="withdrawn"``, that they asked not to be any more).

    Same shape ops writes when an operator records permission; ``method``
    says how it was given — ``consent`` in conversation, ``purchase`` at
    checkout — so it can always be told apart from an operator's say-so.
    Merged into ``platform_meta`` under a row lock: that blob also carries the
    display name, and ops may be writing it at the same moment.  Returns
    ``False`` when the person has no identity on that platform.
    """
    async with session_factory() as db:
        row = (
            await db.execute(
                sa.select(ChannelIdentity)
                .where(ChannelIdentity.org_id == org_id)
                .where(ChannelIdentity.platform == platform)
                .where(ChannelIdentity.platform_user_id == platform_user_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        meta = dict(row.platform_meta or {})
        meta["contact_permission"] = {
            "status": status,
            "recorded_at": _utcnow().replace(tzinfo=None).isoformat(),
            "recorded_by": recorded_by,
            "method": method,
        }
        row.platform_meta = meta
        await db.commit()
        return True


# ---------------------------------------------------------------------------
# Settings and the team
# ---------------------------------------------------------------------------


async def get_settings(
    session_factory: Any, *, org_id: uuid.UUID, agent_id: str,
) -> FollowupSettingsRow | None:
    async with session_factory() as db:
        return await db.get(FollowupSettingsRow, (org_id, agent_id))


def _normalised(field: str, value: Any) -> Any:
    if field in SETTINGS_RANGES:
        if value is None or value == "":
            return None
        low, high = SETTINGS_RANGES[field]
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = None
        if isinstance(value, bool) or number is None or str(number) != str(value).strip() \
                or not low <= number <= high:
            raise ValueError(f"{field} must be a whole number from {low} to {high}")
        return number
    if field == "alert_recipient_user_ids":
        return list(dict.fromkeys(str(u) for u in (value or [])))
    if field == "senders":
        senders = {str(k).strip(): str(v).strip() for k, v in (value or {}).items()}
        _known_platforms(senders)
        return {k: v for k, v in senders.items() if v}
    if field == "team_channels":
        channels = {
            str(uuid.UUID(str(user))): list(dict.fromkeys(str(p).strip() for p in platforms or ()))
            for user, platforms in (value or {}).items()
        }
        for platforms in channels.values():
            _known_platforms(platforms)
        return {user: platforms for user, platforms in channels.items() if platforms}
    if field == "team_permissions":
        return {
            str(uuid.UUID(str(user))): _permission_entry(entry)
            for user, entry in (value or {}).items()
        }
    if field == "team_destinations":
        return [_destination(d) for d in (value or [])]
    if field in ("offer_protocols", "texts"):
        return {
            str(k): str(v).strip() for k, v in (value or {}).items()
            if str(v).strip()
        }
    if isinstance(value, str):
        return value or None
    return value


def _permission_lists(entry: dict) -> dict[str, Any]:
    """``receives`` and ``can`` of an entry, each ``"all"`` or a list."""
    return {key: "all" if entry.get(key) is None else entry[key] for key in ("receives", "can")}


def _permission_entry(entry: Any) -> dict:
    """An entry as it is saved: lists (or ``"all"``) and what they handle."""
    entry = dict(entry or {})
    out: dict[str, Any] = {}
    for (key, items), allowed in zip(_permission_lists(entry).items(), (RECEIVE_KINDS, ABILITIES)):
        if items == "all":
            out[key] = "all"
            continue
        if isinstance(items, str):
            raise ValueError(f"{key} must be \"all\" or a list")
        items = list(dict.fromkeys(str(i) for i in items))
        unknown = set(items) - set(allowed)
        if unknown:
            raise ValueError(f"unknown {key}: {', '.join(sorted(unknown))}")
        out[key] = "all" if set(items) == set(allowed) else items
    handles = str(entry.get("handles") or "").strip()
    if handles:
        out["handles"] = handles[:500]
    return out


def _destination(value: Any) -> dict:
    value = dict(value or {})
    platform = str(value.get("platform") or "").strip()
    channel_id = str(value.get("channel_id") or "").strip()
    caps = capabilities_for(platform)
    if not caps.can_initiate or caps.window_hours or not channel_id:
        raise ValueError("a team destination is a channel id on Slack or Telegram")
    receives = list(dict.fromkeys(str(k) for k in (value.get("receives") or DESTINATION_RECEIVES)))
    unknown = set(receives) - set(RECEIVE_KINDS)
    if unknown:
        raise ValueError(f"unknown receives: {', '.join(sorted(unknown))}")
    out = {"platform": platform, "channel_id": channel_id, "receives": receives}
    name = str(value.get("name") or "").strip()
    return {**out, "name": name} if name else out


def member_permissions(settings: Any, user_id: Any) -> dict[str, Any]:
    """``{receives, can, handles}`` of one team member, the two sets
    spelled out; everything unless their entry says otherwise."""
    entry = ((getattr(settings, "team_permissions", None) or {}) if settings else {}).get(str(user_id)) or {}
    lists = _permission_lists(entry)
    return {
        "receives": frozenset(RECEIVE_KINDS if lists["receives"] == "all" else lists["receives"]),
        "can": frozenset(ABILITIES if lists["can"] == "all" else lists["can"]),
        "handles": str(entry.get("handles") or ""),
    }


def can(settings: Any, user_id: Any, ability: str) -> bool:
    """Whether a team member may do *ability*; nobody off the team may."""
    return is_team_member(settings, user_id) and ability in member_permissions(settings, user_id)["can"]


def _known_platforms(platforms: Iterable[str]) -> None:
    unknown = set(platforms) - set(initiating_platforms())
    if unknown:
        raise ValueError(f"cannot write first on {', '.join(sorted(unknown))}")


def senders_of(settings: Any) -> dict[str, str]:
    """The agent's sender per channel; ``channel_identifier`` is WhatsApp's
    when ``senders`` does not name one."""
    senders = dict(getattr(settings, "senders", None) or {})
    legacy = getattr(settings, "channel_identifier", None)
    if legacy and not senders.get("whatsapp"):
        senders["whatsapp"] = legacy
    return {platform: sender for platform, sender in senders.items() if sender}


async def update_settings(
    session_factory: Any, *, org_id: uuid.UUID, agent_id: str, **fields: Any,
) -> FollowupSettingsRow:
    """Change only the settings fields given; the rest keep their values."""
    unknown = set(fields) - SETTINGS_FIELDS
    if unknown:
        raise ValueError(f"unknown settings fields: {', '.join(sorted(unknown))}")
    fields = {field: _normalised(field, value) for field, value in fields.items()}
    async with session_factory() as db:
        row = await db.get(FollowupSettingsRow, (org_id, agent_id))
        if row is None:
            row = FollowupSettingsRow(
                org_id=org_id, agent_id=agent_id, alert_recipient_user_ids=[],
                offer_protocols={}, texts={},
            )
            db.add(row)
        for field, value in fields.items():
            setattr(row, field, value)
        row.updated_at = _utcnow()
        await db.commit()
        await db.refresh(row)
        return row


def is_team_member(settings: Any, user_id: uuid.UUID | None) -> bool:
    """Whether *user_id* is one of the agent's alert recipients."""
    if settings is None or user_id is None:
        return False
    return str(user_id) in {str(r) for r in (settings.alert_recipient_user_ids or [])}


async def notify_team(
    session_factory: Any,
    settings: Any,
    *,
    org_id: uuid.UUID,
    agent_id: str,
    kind: str,
    text: str,
    created_by: str,
    subject_user_id: uuid.UUID | None = None,
    dedupe_prefix: str | None = None,
    exclude_user_id: uuid.UUID | None = None,
    fallback_senders: dict[str, str] | None = None,
    only_user_id: uuid.UUID | None = None,
    severity: str | None = None,
    reason: str | None = None,
    topic: str | None = None,
    to: Iterable[Any] | None = None,
) -> tuple[int, str | None]:
    """Queue *text* for every alert recipient.  Returns (queued, why none).

    The one place an alert fans out to the team, whatever raised it: the
    agent, a silence streak, a purchase.  Each member is told on the first
    channel of their ``team_channels`` (default WhatsApp, then any) where
    they have a granted identity and the agent has a sender.  The agent's
    ``senders`` win over *fallback_senders* (the sender of the conversation or
    Program that raised it).  With *dedupe_prefix*, each recipient's message
    is keyed on it, so a retried cause never writes twice.  With
    *only_user_id*, the one person it concerns (the team member who asked for
    something) is told instead.  *reason* is what it is about when *text*
    dresses it up (a severity mark, a footer naming the person); the team
    digest lists that.

    *topic* is what kind of message it is (:data:`RECEIVE_KINDS`): only the
    members who receive that kind are told, and the team's shared
    destinations that do.  *to* is the members the agent chose: those of them
    who receive it, else everyone who does.  *only_user_id* always reaches
    its member: it answers their own request.
    """
    if topic is not None and topic not in RECEIVE_KINDS:
        raise ValueError(f"topic must be one of {', '.join(RECEIVE_KINDS)}")
    recipients = list(settings.alert_recipient_user_ids or []) if settings else []
    destinations: list[dict] = []
    if only_user_id is not None:
        recipients = [only_user_id]
    else:
        if topic is not None:
            recipients = [r for r in recipients if topic in member_permissions(settings, r)["receives"]]
        chosen = {str(u) for u in to or ()}
        recipients = [r for r in recipients if str(r) in chosen] or recipients
        destinations = [
            d for d in ((getattr(settings, "team_destinations", None) or []) if settings else [])
            if topic is None or topic in (d.get("receives") or DESTINATION_RECEIVES)
        ]
    if not recipients and not destinations:
        return 0, (
            "no team member receives this kind of message" if topic and settings
            and settings.alert_recipient_user_ids else "no team members are configured"
        )
    senders = {
        platform: sender
        for platform, sender in {**(fallback_senders or {}), **senders_of(settings)}.items()
        if sender and capabilities_for(platform).can_initiate
    }
    ids: list[uuid.UUID] = []
    for recipient in recipients:
        try:
            ids.append(uuid.UUID(str(recipient)))
        except ValueError:
            logger.warning("[followup] ignoring malformed alert recipient %r", recipient)
    ids = [i for i in ids if i != exclude_user_id]  # the team member talking knows
    reachable = await granted_platforms(session_factory, org_id=org_id, user_ids=ids)
    queued, unreached = 0, 0
    for recipient_id in ids:
        order = team_order(settings, recipient_id)
        # Unreachable everywhere still queues on the first channel with a
        # sender: the send pass fails it visibly instead of nobody knowing.
        platform = team_channel_for(
            settings, recipient_id, senders, reachable.get(recipient_id, ()),
        ) or next((p for p in order if p in senders), None)
        if platform is None:
            # No channel of theirs has a sender: recorded as failed, so the
            # history and Studio say which member was not told.
            first = order[0]
            await queue_outreach(
                session_factory, org_id=org_id, agent_id=agent_id,
                user_id=recipient_id, platform=first, channel_identifier="",
                kind=kind, text=text, template_name=None, template_language=None,
                subject_user_id=subject_user_id, created_by=created_by,
                dedupe_key=f"{dedupe_prefix}:{recipient_id}" if dedupe_prefix else None,
                severity=severity, alert_reason=(reason or text).strip(),
                failed_reason=f"{first} is not switched on for this agent: no sender reaches this team member",
            )
            unreached += 1
            continue
        template = capabilities_for(platform).opener == "template" and settings is not None
        await queue_outreach(
            session_factory, org_id=org_id, agent_id=agent_id,
            user_id=recipient_id, platform=platform, channel_identifier=senders[platform],
            kind=kind, text=text,
            template_name=settings.alert_template_name if template else None,
            template_language=settings.alert_template_language if template else None,
            subject_user_id=subject_user_id, created_by=created_by,
            dedupe_key=f"{dedupe_prefix}:{recipient_id}" if dedupe_prefix else None,
            severity=severity, alert_reason=(reason or text).strip(),
        )
        queued += 1
    for destination in destinations:
        platform = destination.get("platform")
        if platform not in senders:
            continue
        await queue_outreach(
            session_factory, org_id=org_id, agent_id=agent_id, user_id=NO_PERSON,
            platform=platform, channel_identifier=senders[platform], kind=kind, text=text,
            template_name=None, template_language=None, subject_user_id=subject_user_id,
            created_by=created_by, destination=destination["channel_id"],
            dedupe_key=f"{dedupe_prefix}:{destination['channel_id']}" if dedupe_prefix else None,
            severity=severity, alert_reason=(reason or text).strip(),
        )
        queued += 1
    missed = (
        f"{unreached} team member(s) could not be reached: none of their channels has a "
        "sender on this agent" if unreached else None
    )
    if queued:
        return queued, missed
    if ids:
        return 0, missed or "no team member can be reached on a channel this agent has a sender for"
    return 0, "the only configured recipient is this person"


def default_team_order() -> list[str]:
    """Where a team member is told when they did not choose: WhatsApp, then any."""
    return ["whatsapp", *(p for p in initiating_platforms() if p != "whatsapp")]


def team_order(settings: Any, user_id: Any) -> list[str]:
    """A team member's channels, best first: their own ``team_channels``,
    then every other, so a member whose chosen channel has no sender is
    still told on another."""
    own = ((getattr(settings, "team_channels", None) or {}) if settings else {}).get(str(user_id))
    return list(dict.fromkeys([*(own or ()), *default_team_order()]))


def team_channel_for(
    settings: Any, user_id: Any, senders: dict[str, str], reachable: Iterable[str],
) -> str | None:
    """The channel a team member is told on: the first of :func:`team_order`
    with a sender of the agent's that they can be reached on (*reachable*,
    see :func:`granted_platforms`)."""
    reachable = set(reachable)
    return next((p for p in team_order(settings, user_id) if p in senders and p in reachable), None)


async def granted_platforms(
    session_factory: Any, *, org_id: uuid.UUID, user_ids: list[uuid.UUID],
) -> dict[uuid.UUID, set[str]]:
    """Each person's channels where they may be messaged first."""
    if not user_ids:
        return {}
    async with session_factory() as db:
        rows = (
            await db.execute(
                sa.select(ChannelIdentity)
                .where(ChannelIdentity.org_id == org_id)
                .where(ChannelIdentity.user_id.in_(user_ids))
            )
        ).scalars().all()
    out: dict[uuid.UUID, set[str]] = {}
    for row in rows:
        if permission_status(row) == "granted":
            out.setdefault(row.user_id, set()).add(row.platform)
    return out


# ---------------------------------------------------------------------------
# Channel contacts
# ---------------------------------------------------------------------------


async def record_inbound(
    session_factory: Any,
    *,
    org_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
    channel_identifier: str,
    at: datetime | None = None,
) -> None:
    """Remember that this person just wrote to this sender — one upsert."""
    values = dict(
        org_id=org_id, platform=platform, platform_user_id=platform_user_id,
        channel_identifier=channel_identifier, last_inbound_at=at or _utcnow(),
    )
    async with session_factory() as db:
        insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
        stmt = insert(ChannelContactRow).values(**values)
        await db.execute(
            stmt.on_conflict_do_update(
                index_elements=[
                    ChannelContactRow.org_id, ChannelContactRow.platform,
                    ChannelContactRow.platform_user_id,
                    ChannelContactRow.channel_identifier,
                ],
                set_={"last_inbound_at": stmt.excluded.last_inbound_at},
            )
        )
        await db.commit()


async def last_inbound(
    session_factory: Any,
    *,
    org_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
    channel_identifier: str,
) -> datetime | None:
    async with session_factory() as db:
        row = await db.get(
            ChannelContactRow,
            (org_id, platform, platform_user_id, channel_identifier),
        )
        return row.last_inbound_at if row is not None else None


async def last_inbound_by_user(
    session_factory: Any,
    *,
    org_id: uuid.UUID,
    platform: str,
    channel_identifier: str,
    platform_user_ids: Iterable[str],
) -> dict[str, datetime]:
    ids = list(platform_user_ids)
    if not ids:
        return {}
    async with session_factory() as db:
        rows = (
            await db.execute(
                sa.select(
                    ChannelContactRow.platform_user_id,
                    ChannelContactRow.last_inbound_at,
                )
                .where(ChannelContactRow.org_id == org_id)
                .where(ChannelContactRow.platform == platform)
                .where(ChannelContactRow.channel_identifier == channel_identifier)
                .where(ChannelContactRow.platform_user_id.in_(ids))
            )
        ).all()
    return {pid: at for pid, at in rows}


async def last_inbound_by_person(
    session_factory: Any, *, org_id: uuid.UUID, user_ids: Iterable[uuid.UUID],
    senders: Iterable[str] | None = None,
) -> dict[uuid.UUID, datetime]:
    """When each person last wrote to the business, on any channel; to one
    of *senders* when given, else to any sender."""
    ids = list(user_ids)
    senders = None if senders is None else set(senders)
    if not ids or senders == set():
        return {}
    async with session_factory() as db:
        rows = (
            await db.execute(
                sa.select(ChannelIdentity.user_id, sa.func.max(ChannelContactRow.last_inbound_at))
                .join(ChannelContactRow, sa.and_(
                    ChannelContactRow.platform == ChannelIdentity.platform,
                    ChannelContactRow.platform_user_id == ChannelIdentity.platform_user_id,
                ))
                .where(ChannelIdentity.org_id == org_id)
                .where(ChannelContactRow.org_id == org_id)
                .where(ChannelIdentity.user_id.in_(ids))
                .where(sa.true() if senders is None else ChannelContactRow.channel_identifier.in_(senders))
                .group_by(ChannelIdentity.user_id)
            )
        ).all()
    return {uid: at for uid, at in rows if at is not None}


# ---------------------------------------------------------------------------
# Outreach
# ---------------------------------------------------------------------------


async def queue_outreach(
    session_factory: Any,
    *,
    org_id: uuid.UUID,
    agent_id: str,
    user_id: uuid.UUID,
    platform: str,
    channel_identifier: str,
    kind: str,
    text: str,
    template_name: str | None,
    template_language: str | None,
    subject_user_id: uuid.UUID | None = None,
    created_by: str | None = None,
    dedupe_key: str | None = None,
    severity: str | None = None,
    alert_reason: str | None = None,
    destination: str | None = None,
    send_now: bool = False,
    failed_reason: str | None = None,
) -> OutreachMessageRow:
    """Queue one message.  With ``dedupe_key``, queuing the same cause twice
    returns the first row instead of writing to the person again.

    A team member's message to a person (``operator``) is held through the
    person's quiet hours: the row's ``not_before`` says until when, for the
    caller to tell the sender.  *send_now* sends it anyway.  Alerts to the
    team are never held.

    The only gate on platform: a row for a channel outreach cannot reach is
    refused here, so nothing downstream has to check again.
    """
    if kind not in OUTREACH_KINDS:
        raise ValueError(f"kind must be one of {', '.join(OUTREACH_KINDS)}")
    if severity is not None and severity not in ALERT_SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(ALERT_SEVERITIES)}")
    if not capabilities_for(platform).can_initiate:
        raise ValueError(f"outreach is not available on {platform}")
    text = text.strip()
    if not text:
        raise ValueError("an outreach message needs text")
    if kind == "operator" and (created_by or "").startswith("team:"):
        # A team member writing to the same person again (reworded, or "send
        # it now") replaces what of theirs is still held: the person must not
        # get the stale one when the quiet hours end.
        async with session_factory() as db:
            await db.execute(
                sa.update(OutreachMessageRow)
                .where(OutreachMessageRow.org_id == org_id)
                .where(OutreachMessageRow.agent_id == agent_id)
                .where(OutreachMessageRow.created_by == created_by)
                .where(OutreachMessageRow.kind == "operator")
                .where(OutreachMessageRow.state == "queued")
                .where(OutreachMessageRow.not_before.isnot(None))
                .where(sa.or_(
                    OutreachMessageRow.subject_user_id == (subject_user_id or user_id),
                    OutreachMessageRow.user_id == user_id,
                ))
                .values(state="replaced", reason="Replaced by a newer message from the same team member")
            )
            await db.commit()
    not_before = None
    if kind == "operator" and not send_now:
        from surogates.programs.journey import quiet_until

        held = await quiet_until(
            session_factory, org_id=org_id, agent_id=agent_id, user_id=user_id,
            now=datetime.now(timezone.utc),
        )
        not_before = held[0] if held else None
    async with session_factory() as db:
        row = OutreachMessageRow(
            org_id=org_id, agent_id=agent_id, user_id=user_id,
            platform=platform, channel_identifier=channel_identifier,
            kind=kind, text=text, template_name=template_name or None,
            template_language=template_language or None,
            # Recorded, not sent, when nothing could carry it (*failed_reason*).
            state="failed" if failed_reason else "queued", reason=failed_reason,
            subject_user_id=subject_user_id, created_by=created_by,
            dedupe_key=dedupe_key, severity=severity, alert_reason=alert_reason,
            destination=destination or None,
            not_before=not_before,
        )
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            if dedupe_key is None:
                raise
            return (
                await db.execute(
                    sa.select(OutreachMessageRow)
                    .where(OutreachMessageRow.org_id == org_id)
                    .where(OutreachMessageRow.dedupe_key == dedupe_key)
                )
            ).scalar_one()
        await db.refresh(row)
        return row


# ---------------------------------------------------------------------------
# Escalations
# ---------------------------------------------------------------------------


async def record_escalation(
    session_factory: Any, *, org_id: uuid.UUID, agent_id: str, user_id: uuid.UUID | None,
    kind: str, reason: str, state: str = "raised", severity: str | None = None,
    platform: str | None = None, invitation_id: uuid.UUID | None = None,
    program_id: uuid.UUID | None = None, session_id: uuid.UUID | None = None,
    escalation_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Write one escalation for the team's queue.  With *escalation_id*, a
    retried cause updates its record instead of adding a second one."""
    escalation_id = escalation_id or uuid.uuid4()
    async with session_factory() as db:
        row = await db.get(EscalationRow, escalation_id)
        if row is None:
            row = EscalationRow(
                id=escalation_id, org_id=org_id, agent_id=agent_id, user_id=user_id,
            )
            db.add(row)
        row.kind, row.reason, row.state, row.severity = kind, reason, state, severity
        row.platform, row.session_id = platform, session_id
        row.invitation_id, row.program_id = invitation_id, program_id
        await db.commit()
    return escalation_id


async def _escalation(
    session_factory: Any, org_id: uuid.UUID, agent_id: str, escalation_id: uuid.UUID, **values: Any,
) -> EscalationRow | None:
    """Set *values* on one of the agent's escalations; ``None`` when there
    is no such escalation."""
    async with session_factory() as db:
        row = await db.get(EscalationRow, escalation_id)
        if row is None or row.org_id != org_id or row.agent_id != agent_id:
            return None
        for field, value in values.items():
            setattr(row, field, value)
        await db.commit()
        await db.refresh(row)
        return row


async def claim_escalation(
    session_factory: Any, *, org_id: uuid.UUID, agent_id: str,
    escalation_id: uuid.UUID, owner: str,
) -> EscalationRow | None:
    """Make *owner* (a team member's id or name) the one dealing with it."""
    owner = (owner or "").strip()
    if not owner:
        raise ValueError("an escalation needs an owner")
    return await _escalation(session_factory, org_id, agent_id, escalation_id, owner=owner)


async def resolve_escalation(
    session_factory: Any, *, org_id: uuid.UUID, agent_id: str,
    escalation_id: uuid.UUID, resolved: bool = True,
) -> EscalationRow | None:
    """Mark it dealt with, or open it again with ``resolved=False``.  Dealt
    with, it also leaves the Inbox (see :func:`close_escalation_inbox_items`),
    whoever resolved it, in Studio or in chat."""
    row = await _escalation(
        session_factory, org_id, agent_id, escalation_id,
        resolved_at=_utcnow() if resolved else None,
    )
    if row is not None and resolved:
        await close_escalation_inbox_items(session_factory, org_id=org_id, escalation_id=escalation_id)
    return row


async def close_escalation_inbox_items(
    session_factory: Any, *, org_id: uuid.UUID, escalation_id: uuid.UUID,
) -> int:
    """Close the Inbox items an escalation raised, now that it is resolved.

    Acknowledged, not responded: responding to an action posts a message into
    the conversation, and an escalation's conversation is the person's own.
    The item names its escalation as ``escalation:<step or ->:<id hex>``.
    """
    now = _utcnow()
    async with session_factory() as db:
        result = await db.execute(
            sa.update(InboxItem)
            .where(InboxItem.org_id == org_id)
            .where(InboxItem.status == "pending")
            .where(InboxItem.payload["escalation"].as_string().like(f"%:{escalation_id.hex}"))
            .values(status="acknowledged", responded_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
    return int(result.rowcount or 0)


# ---------------------------------------------------------------------------
# Followed people and their contacts
# ---------------------------------------------------------------------------


class Recipient(NamedTuple):
    """Who a step for one followed person goes to."""

    user_id: uuid.UUID
    role: str  # "person" or "contact"
    relation: str | None


class Speaker(NamedTuple):
    """Who is writing, for whom."""

    person_user_id: uuid.UUID
    person_name: str
    name: str
    role: str  # "person" or "contact"
    relation: str | None


async def contacts(
    session_factory: Any,
    *,
    org_id: uuid.UUID,
    agent_id: str,
    person_user_id: uuid.UUID | None = None,
    contact_user_id: uuid.UUID | None = None,
    status: str | None = "confirmed",
) -> list[FollowupContactRow]:
    """Links of this agent, oldest first, narrowed by whichever filters are given."""
    query = (
        sa.select(FollowupContactRow)
        .where(FollowupContactRow.org_id == org_id)
        .where(FollowupContactRow.agent_id == agent_id)
        .order_by(FollowupContactRow.created_at, FollowupContactRow.id)
    )
    for column, value in (
        (FollowupContactRow.person_user_id, person_user_id),
        (FollowupContactRow.contact_user_id, contact_user_id),
        (FollowupContactRow.status, status),
    ):
        if value is not None:
            query = query.where(column == value)
    async with session_factory() as db:
        return list((await db.execute(query)).scalars().all())


async def recipients_for(session_factory: Any, enrollment: Any) -> list[Recipient]:
    """The followed person, then each confirmed contact: who a step goes to."""
    linked = await contacts(
        session_factory, org_id=enrollment.org_id, agent_id=enrollment.agent_id,
        person_user_id=enrollment.user_id,
    )
    return [Recipient(enrollment.user_id, "person", None)] + [
        Recipient(c.contact_user_id, "contact", c.relation)
        for c in linked if c.contact_user_id != enrollment.user_id
    ]


async def link_contact(
    session_factory: Any,
    *,
    org_id: uuid.UUID,
    agent_id: str,
    person_user_id: uuid.UUID,
    contact_user_id: uuid.UUID,
    relation: str | None,
    created_by: str,
    status: str = "confirmed",
    asked_user_id: uuid.UUID | None = None,
) -> FollowupContactRow:
    """Link *contact_user_id* to the person, or update an existing link.

    A confirmed link is never turned back into a pending one.  Raises
    :class:`TeamMember` for a member of the team and ValueError for the
    person themselves.
    """
    if status not in ("confirmed", "pending"):
        raise ValueError("status must be confirmed or pending")
    if contact_user_id == person_user_id:
        raise ValueError("the person cannot be their own contact")
    settings = await get_settings(session_factory, org_id=org_id, agent_id=agent_id)
    if is_team_member(settings, contact_user_id):
        raise TeamMember("this person is on the team, not a contact")
    relation = (relation or "").strip()[:200] or None
    for attempt in (1, 2):
        async with session_factory() as db:
            row = (
                await db.execute(
                    sa.select(FollowupContactRow)
                    .where(FollowupContactRow.org_id == org_id)
                    .where(FollowupContactRow.agent_id == agent_id)
                    .where(FollowupContactRow.person_user_id == person_user_id)
                    .where(FollowupContactRow.contact_user_id == contact_user_id)
                )
            ).scalar_one_or_none()
            if row is None:
                row = FollowupContactRow(
                    org_id=org_id, agent_id=agent_id, person_user_id=person_user_id,
                    contact_user_id=contact_user_id, created_by=created_by,
                    status=status, created_at=_utcnow(),
                )
                db.add(row)
            elif status == "confirmed":
                row.status = "confirmed"
            if relation:
                row.relation = relation
            row.asked_user_id = asked_user_id if row.status == "pending" else None
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                if attempt == 2:
                    raise
                continue
            await db.refresh(row)
            return row
    raise AssertionError("unreachable")


async def remove_contact(
    session_factory: Any, *, org_id: uuid.UUID, agent_id: str,
    person_user_id: uuid.UUID, contact_user_id: uuid.UUID,
) -> bool:
    async with session_factory() as db:
        result = await db.execute(
            sa.delete(FollowupContactRow)
            .where(FollowupContactRow.org_id == org_id)
            .where(FollowupContactRow.agent_id == agent_id)
            .where(FollowupContactRow.person_user_id == person_user_id)
            .where(FollowupContactRow.contact_user_id == contact_user_id)
        )
        await db.commit()
    return int(result.rowcount or 0) > 0


async def display_names(session_factory: Any, user_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    ids = list(dict.fromkeys(user_ids))
    if not ids:
        return {}
    async with session_factory() as db:
        return {
            uid: name or ""
            for uid, name in (
                await db.execute(sa.select(User.id, User.display_name).where(User.id.in_(ids)))
            ).all()
        }


async def person_name(session_factory: Any, *, org_id: uuid.UUID, agent_id: str, user_id: uuid.UUID) -> str:
    """The followed person's own name: their enrollment's, else their account's."""
    row = labelling_enrollment(
        await enrollments(session_factory, org_id=org_id, agent_id=agent_id, user_id=user_id)
    )
    if row is not None and row.patient_name:
        return row.patient_name
    return (await display_names(session_factory, [user_id])).get(user_id, "")


async def speaker_for(
    session_factory: Any,
    *,
    org_id: uuid.UUID,
    agent_id: str,
    user_id: uuid.UUID | None,
    person_user_id: uuid.UUID | None = None,
) -> Speaker | None:
    """Who *user_id* is to this agent's follow-up: the followed person, or a
    confirmed contact of one (*person_user_id* picks which, else the most
    recent link).  ``None`` for anyone else."""
    if user_id is None:
        return None
    own = await enrollments(session_factory, org_id=org_id, agent_id=agent_id, user_id=user_id)
    links = await contacts(session_factory, org_id=org_id, agent_id=agent_id, contact_user_id=user_id)
    if own and (person_user_id is None or person_user_id == user_id):
        name = await person_name(session_factory, org_id=org_id, agent_id=agent_id, user_id=user_id)
        return Speaker(user_id, name, name, "person", None)
    if person_user_id is not None:
        links = [c for c in links if c.person_user_id == person_user_id]
    if not links:
        return None
    link = links[-1]
    names = await display_names(session_factory, [user_id])
    return Speaker(
        link.person_user_id,
        await person_name(session_factory, org_id=org_id, agent_id=agent_id, user_id=link.person_user_id),
        names.get(user_id, ""), "contact", link.relation,
    )


def writer_line(speaker: Speaker | None) -> str:
    """Who is writing, as the agent reads it in its context."""
    if speaker is None:
        return ""
    if speaker.role == "person":
        return f"Writing: {speaker.person_name or 'the person'} (the person followed)"
    who = speaker.name or "a contact"
    return f"Writing: {who} ({speaker.relation or 'contact'}), for {speaker.person_name or 'the person followed'}"


#: Channels whose one-to-one conversations keep a memory partition of their own.
_PARTITIONED = ("whatsapp", "telegram", "slack", "voice")


async def shared_memory_boundary(
    session_factory: Any, *, org_id: uuid.UUID, agent_id: str, user_id: uuid.UUID | None,
) -> str | None:
    """The memory partition a followed person's record lives in, for a
    one-to-one conversation of theirs or of a contact; ``None`` for anyone
    else, who keeps the conversation's own.

    Chosen once and kept on the person's enrollments: the partition of the
    conversation already holding their record (their own newest, else their
    contacts' in link order), so nothing written before is left behind.
    """
    if session_factory is None:
        return None
    speaker = await speaker_for(session_factory, org_id=org_id, agent_id=agent_id, user_id=user_id)
    if speaker is None:
        return None
    person = speaker.person_user_id
    rows = await enrollments(session_factory, org_id=org_id, agent_id=agent_id, user_id=person)
    found = next((r.memory_boundary for r in rows if r.memory_boundary), None)
    if found or not rows:
        return found
    linked = await contacts(session_factory, org_id=org_id, agent_id=agent_id, person_user_id=person)
    async with session_factory() as db:
        for candidate in [person, *(c.contact_user_id for c in linked)]:
            configs = (
                await db.execute(
                    sa.select(SessionRow.config)
                    .where(SessionRow.org_id == org_id)
                    .where(SessionRow.agent_id == agent_id)
                    .where(SessionRow.user_id == candidate)
                    .where(SessionRow.channel.in_(_PARTITIONED))
                    .order_by(SessionRow.created_at.desc())
                )
            ).scalars().all()
            found = next(
                (
                    str(c.get("memory_boundary")) for c in configs
                    if (c or {}).get("memory_boundary") and not c.get("multi_party")
                    and str(c.get("memory_boundary")) != "public"
                ),
                None,
            )
            if found:
                break
        found = found or f"person:{person}"
        await db.execute(
            sa.update(FollowupEnrollmentRow)
            .where(FollowupEnrollmentRow.org_id == org_id)
            .where(FollowupEnrollmentRow.agent_id == agent_id)
            .where(FollowupEnrollmentRow.user_id == person)
            .where(FollowupEnrollmentRow.memory_boundary.is_(None))
            .values(memory_boundary=found)
        )
        await db.commit()
    return found


# The names ops imports from a pinned runtime wheel; kept until ops moves.
CareTeamMember = TeamMember
is_care_team = is_team_member
notify_care_team = notify_team
patient_label = person_label
