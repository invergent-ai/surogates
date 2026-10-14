"""Reads and writes for the follow-up tables.

Every function takes a session factory, so the runtime and ops (which imports
this package against the same database) share one implementation.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any, Iterable
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
    FollowupEnrollmentRow,
    FollowupSettingsRow,
    OutreachMessageRow,
    ProgramScheduleRow,
)
from surogates.followup.eligibility import Audience

logger = logging.getLogger(__name__)

__all__ = [
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
ENROLLMENT_SOURCES = ("agent", "operator", "purchase", "team")
OUTREACH_KINDS = ("alert", "operator", "enrolment", "welcome", "notice")

#: Settings fields :func:`update_settings` accepts.
SETTINGS_FIELDS = frozenset({
    "escalation_service_account_id", "channel_identifier",
    "alert_recipient_user_ids", "senders", "team_channels", "alert_template_name",
    "alert_template_language", "message_template_name",
    "message_template_language", "welcome_template_name",
    "welcome_template_language", "offer_protocols", "texts",
    "operator_unanswered_hours", "red_alert_reminder_minutes",
})
ALERT_SEVERITIES = ("red", "yellow")
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


def person_label(display_name: str | None, enrollment: Any = None) -> str:
    """Who an enrollment is about, as the team knows them.

    The enrollment's person name wins over *display_name*: that is the
    WhatsApp profile of whoever is writing, often a relative.
    """
    name = (getattr(enrollment, "patient_name", None) or display_name or "").strip()
    parts = [name] if name else []
    answered_by = getattr(enrollment, "answered_by", None)
    if answered_by:
        parts.append(f"via {answered_by}")
    return " · ".join(parts)


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
) -> bool:
    """Record that the person consented to be messaged.

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
            "status": "granted",
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
    if field in ("offer_protocols", "texts"):
        return {
            str(k): str(v).strip() for k, v in (value or {}).items()
            if str(v).strip()
        }
    if isinstance(value, str):
        return value or None
    return value


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
    """
    recipients = list(settings.alert_recipient_user_ids or []) if settings else []
    if only_user_id is not None:
        recipients = [only_user_id]
    if not recipients:
        return 0, "no team members are configured"
    senders = {
        platform: sender
        for platform, sender in {**(fallback_senders or {}), **senders_of(settings)}.items()
        if sender and capabilities_for(platform).can_initiate
    }
    if not senders:
        return 0, "no sender is configured for alerts"
    ids: list[uuid.UUID] = []
    for recipient in recipients:
        try:
            ids.append(uuid.UUID(str(recipient)))
        except ValueError:
            logger.warning("[followup] ignoring malformed alert recipient %r", recipient)
    ids = [i for i in ids if i != exclude_user_id]  # the team member talking knows
    reachable = await _granted_platforms(session_factory, org_id=org_id, user_ids=ids)
    team_channels = (getattr(settings, "team_channels", None) or {}) if settings else {}
    default_order = ["whatsapp", *(p for p in initiating_platforms() if p != "whatsapp")]
    queued = 0
    for recipient_id in ids:
        order = team_channels.get(str(recipient_id)) or default_order
        # Unreachable everywhere still queues on the first channel with a
        # sender: the send pass fails it visibly instead of nobody knowing.
        platform = next(
            (p for p in order if p in senders and p in reachable.get(recipient_id, ())),
            next((p for p in order if p in senders), None),
        )
        if platform is None:
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
    if queued:
        return queued, None
    if ids:
        return 0, "no team member can be reached on a channel this agent has a sender for"
    return 0, "the only configured recipient is this person"


async def _granted_platforms(
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
) -> dict[uuid.UUID, datetime]:
    """When each person last wrote to the business, on any channel and sender."""
    ids = list(user_ids)
    if not ids:
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
) -> OutreachMessageRow:
    """Queue one message.  With ``dedupe_key``, queuing the same cause twice
    returns the first row instead of writing to the person again.

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
    async with session_factory() as db:
        row = OutreachMessageRow(
            org_id=org_id, agent_id=agent_id, user_id=user_id,
            platform=platform, channel_identifier=channel_identifier,
            kind=kind, text=text, template_name=template_name or None,
            template_language=template_language or None, state="queued",
            subject_user_id=subject_user_id, created_by=created_by,
            dedupe_key=dedupe_key, severity=severity, alert_reason=alert_reason,
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
    session_factory: Any, *, org_id: uuid.UUID, agent_id: str, user_id: uuid.UUID,
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
    """Mark it dealt with, or open it again with ``resolved=False``."""
    return await _escalation(
        session_factory, org_id, agent_id, escalation_id,
        resolved_at=_utcnow() if resolved else None,
    )


# The names ops imports from a pinned runtime wheel; kept until ops moves.
CareTeamMember = TeamMember
is_care_team = is_team_member
notify_care_team = notify_team
patient_label = person_label
