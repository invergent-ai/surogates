"""Reads and writes for the follow-up tables.

Every function takes a session factory, so the runtime and ops (which imports
this package against the same database) share one implementation.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any, Iterable

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from surogates.db.models import (
    ChannelContactRow,
    ChannelIdentity,
    FollowupEnrollmentRow,
    FollowupSettingsRow,
    OutreachMessageRow,
    ProgramScheduleRow,
)
from surogates.followup.eligibility import Audience

logger = logging.getLogger(__name__)

__all__ = [
    "CareTeamMember",
    "ENROLLMENT_SOURCES",
    "ENROLLMENT_STATUSES",
    "OUTREACH_KINDS",
    "OUTREACH_PLATFORMS",
    "SETTINGS_FIELDS",
    "active_program_configs",
    "enrollments",
    "get_settings",
    "grant_contact_permission",
    "is_care_team",
    "labelling_enrollment",
    "last_inbound",
    "last_inbound_by_user",
    "notify_care_team",
    "patient_label",
    "permission_status",
    "protocol_timezones",
    "queue_outreach",
    "record_inbound",
    "render_text",
    "update_enrollment",
    "update_settings",
    "upsert_enrollment",
]

ENROLLMENT_STATUSES = ("active", "paused", "ended")
ENROLLMENT_SOURCES = ("agent", "operator", "purchase")
OUTREACH_KINDS = ("alert", "operator", "enrolment", "welcome")
#: Where outreach can reach a person.  WhatsApp only: it is the one channel
#: whose rules (a template outside the 24-hour window) outreach implements.
OUTREACH_PLATFORMS = ("whatsapp",)

#: Settings fields :func:`update_settings` accepts.
SETTINGS_FIELDS = frozenset({
    "escalation_service_account_id", "channel_identifier",
    "alert_recipient_user_ids", "alert_template_name",
    "alert_template_language", "message_template_name",
    "message_template_language", "welcome_template_name",
    "welcome_template_language", "offer_protocols", "texts",
})


class CareTeamMember(ValueError):
    """The person is an alert recipient of this agent, not a patient."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def render_text(custom: str | None, default: str, **values: Any) -> str:
    """The practice's wording with *values* filled in, else the default.

    A stray brace or an unknown placeholder in the practice's text must not
    cost the message: it falls back to the platform's own sentence.
    """
    text = (custom or "").strip() or default
    try:
        return text.format(**values)
    except (KeyError, IndexError, ValueError):
        logger.warning("[followup] unusable practice wording; using the default")
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
) -> FollowupEnrollmentRow:
    """Enrol a person, or refresh and reactivate their existing enrollment.

    Enrolling again is how a person comes back after an ended programme, so
    an existing row is reactivated rather than refused.  A ``None`` start
    date or phase leaves what is recorded untouched: re-enrolling someone
    without restating the date must not erase it; the same goes for the
    patient's name and who answers.

    Raises :class:`CareTeamMember` for an alert recipient of this agent —
    whichever path tries it: the agent in conversation, Studio, or a checkout
    that was given the doctor's own number.
    """
    if enrolled_by not in ENROLLMENT_SOURCES:
        raise ValueError(f"enrolled_by must be one of {', '.join(ENROLLMENT_SOURCES)}")
    settings = await get_settings(session_factory, org_id=org_id, agent_id=agent_id)
    if is_care_team(settings, user_id):
        raise CareTeamMember("this person is on the care team, not a patient")
    protocol = protocol.strip()
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
) -> FollowupEnrollmentRow | None:
    """Change an enrollment's fields; only the arguments passed are touched.

    ``None`` is a real value here (clear the phase, forget the start date),
    which is why "not passed" has its own sentinel.  Returns ``None`` when
    the person is not enrolled in that protocol.
    """
    if status is not _UNSET and status not in ENROLLMENT_STATUSES:
        raise ValueError(f"status must be one of {', '.join(ENROLLMENT_STATUSES)}")
    async with session_factory() as db:
        row = (
            await db.execute(_one_enrollment(org_id, agent_id, user_id, protocol))
        ).scalar_one_or_none()
        if row is None:
            return None
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


def patient_label(display_name: str | None, enrollment: Any = None) -> str:
    """Who an enrollment is about, as the care team knows them.

    The enrollment's patient name wins over *display_name*: that is the
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
# Settings and the care team
# ---------------------------------------------------------------------------


async def get_settings(
    session_factory: Any, *, org_id: uuid.UUID, agent_id: str,
) -> FollowupSettingsRow | None:
    async with session_factory() as db:
        return await db.get(FollowupSettingsRow, (org_id, agent_id))


def _normalised(field: str, value: Any) -> Any:
    if field == "alert_recipient_user_ids":
        return list(dict.fromkeys(str(u) for u in (value or [])))
    if field in ("offer_protocols", "texts"):
        return {
            str(k): str(v).strip() for k, v in (value or {}).items()
            if str(v).strip()
        }
    if isinstance(value, str):
        return value or None
    return value


async def update_settings(
    session_factory: Any, *, org_id: uuid.UUID, agent_id: str, **fields: Any,
) -> FollowupSettingsRow:
    """Change only the settings fields given; the rest keep their values."""
    unknown = set(fields) - SETTINGS_FIELDS
    if unknown:
        raise ValueError(f"unknown settings fields: {', '.join(sorted(unknown))}")
    async with session_factory() as db:
        row = await db.get(FollowupSettingsRow, (org_id, agent_id))
        if row is None:
            row = FollowupSettingsRow(
                org_id=org_id, agent_id=agent_id, alert_recipient_user_ids=[],
                offer_protocols={}, texts={},
            )
            db.add(row)
        for field, value in fields.items():
            setattr(row, field, _normalised(field, value))
        row.updated_at = _utcnow()
        await db.commit()
        await db.refresh(row)
        return row


def is_care_team(settings: Any, user_id: uuid.UUID | None) -> bool:
    """Whether *user_id* is one of the agent's alert recipients."""
    if settings is None or user_id is None:
        return False
    return str(user_id) in {str(r) for r in (settings.alert_recipient_user_ids or [])}


async def notify_care_team(
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
    fallback_sender: str | None = None,
) -> tuple[int, str | None]:
    """Queue *text* for every alert recipient.  Returns (queued, why none).

    The one place an alert fans out to the care team's phones, whatever
    raised it: the agent, a silence streak, a purchase.  With
    *dedupe_prefix*, each recipient's message is keyed on it, so a retried
    cause never writes twice.
    """
    recipients = list(settings.alert_recipient_user_ids or []) if settings else []
    if not recipients:
        return 0, "no care-team phones are configured"
    sender = (settings.channel_identifier if settings else None) or fallback_sender
    if not sender:
        return 0, "no WhatsApp sender is configured for alerts"
    queued = 0
    for recipient in recipients:
        try:
            recipient_id = uuid.UUID(str(recipient))
        except ValueError:
            logger.warning("[followup] ignoring malformed alert recipient %r", recipient)
            continue
        if recipient_id == exclude_user_id:
            continue  # the care-team member is the one talking; they know
        await queue_outreach(
            session_factory, org_id=org_id, agent_id=agent_id,
            user_id=recipient_id, platform="whatsapp", channel_identifier=sender,
            kind=kind, text=text, template_name=settings.alert_template_name,
            template_language=settings.alert_template_language,
            subject_user_id=subject_user_id, created_by=created_by,
            dedupe_key=f"{dedupe_prefix}:{recipient_id}" if dedupe_prefix else None,
        )
        queued += 1
    return queued, None if queued else "the only configured recipient is this person"


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
) -> OutreachMessageRow:
    """Queue one message.  With ``dedupe_key``, queuing the same cause twice
    returns the first row instead of writing to the person again.

    The only gate on platform: a row for a channel outreach cannot reach is
    refused here, so nothing downstream has to check again.
    """
    if kind not in OUTREACH_KINDS:
        raise ValueError(f"kind must be one of {', '.join(OUTREACH_KINDS)}")
    if platform not in OUTREACH_PLATFORMS:
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
            dedupe_key=dedupe_key,
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
