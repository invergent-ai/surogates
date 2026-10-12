"""Reads and writes for the follow-up tables.

Every function takes a session factory, so the runtime and ops (which imports
this package against the same database) share one implementation.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any, Iterable

import sqlalchemy as sa
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

__all__ = [
    "ENROLLMENT_SOURCES",
    "ENROLLMENT_STATUSES",
    "OUTREACH_KINDS",
    "OUTREACH_PLATFORMS",
    "active_enrollments",
    "enrollments_for_agent",
    "enrollments_for_user",
    "get_settings",
    "grant_contact_permission",
    "last_inbound",
    "last_inbound_by_user",
    "offered_protocols",
    "protocol_timezones",
    "queue_outreach",
    "record_inbound",
    "save_settings",
    "update_enrollment",
    "upsert_enrollment",
]

ENROLLMENT_STATUSES = ("active", "paused", "ended")
ENROLLMENT_SOURCES = ("agent", "operator", "purchase")
OUTREACH_KINDS = ("alert", "operator", "enrolment", "welcome")
#: Where outreach can reach a person.  WhatsApp only: it is the one channel
#: whose rules (a template outside the 24-hour window) this module implements.
OUTREACH_PLATFORMS = ("whatsapp",)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Enrollments
# ---------------------------------------------------------------------------


async def active_enrollments(
    session_factory: Any, *, org_id: uuid.UUID, agent_id: str, protocol: str,
) -> list[FollowupEnrollmentRow]:
    async with session_factory() as db:
        return list(
            (
                await db.execute(
                    sa.select(FollowupEnrollmentRow)
                    .where(FollowupEnrollmentRow.org_id == org_id)
                    .where(FollowupEnrollmentRow.agent_id == agent_id)
                    .where(FollowupEnrollmentRow.protocol == protocol)
                    .where(FollowupEnrollmentRow.status == "active")
                    .order_by(FollowupEnrollmentRow.created_at)
                )
            ).scalars().all()
        )


async def enrollments_for_user(
    session_factory: Any, *, org_id: uuid.UUID, agent_id: str, user_id: uuid.UUID,
) -> list[FollowupEnrollmentRow]:
    async with session_factory() as db:
        return list(
            (
                await db.execute(
                    sa.select(FollowupEnrollmentRow)
                    .where(FollowupEnrollmentRow.org_id == org_id)
                    .where(FollowupEnrollmentRow.agent_id == agent_id)
                    .where(FollowupEnrollmentRow.user_id == user_id)
                    .order_by(FollowupEnrollmentRow.created_at)
                )
            ).scalars().all()
        )


async def enrollments_for_agent(
    session_factory: Any, *, org_id: uuid.UUID, agent_id: str,
) -> list[FollowupEnrollmentRow]:
    async with session_factory() as db:
        return list(
            (
                await db.execute(
                    sa.select(FollowupEnrollmentRow)
                    .where(FollowupEnrollmentRow.org_id == org_id)
                    .where(FollowupEnrollmentRow.agent_id == agent_id)
                    .order_by(
                        FollowupEnrollmentRow.status,
                        FollowupEnrollmentRow.created_at.desc(),
                    )
                )
            ).scalars().all()
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
) -> FollowupEnrollmentRow:
    """Enrol a person, or refresh and reactivate their existing enrollment.

    Enrolling again is how a person comes back after an ended programme, so
    an existing row is reactivated rather than refused.  A ``None`` start
    date or phase leaves what is recorded untouched: the agent re-enrolling
    someone without restating the date must not erase it.
    """
    if enrolled_by not in ENROLLMENT_SOURCES:
        raise ValueError(f"enrolled_by must be one of {', '.join(ENROLLMENT_SOURCES)}")
    protocol = protocol.strip()
    for attempt in (1, 2):
        async with session_factory() as db:
            row = (
                await db.execute(
                    sa.select(FollowupEnrollmentRow)
                    .where(FollowupEnrollmentRow.org_id == org_id)
                    .where(FollowupEnrollmentRow.agent_id == agent_id)
                    .where(FollowupEnrollmentRow.user_id == user_id)
                    .where(FollowupEnrollmentRow.protocol == protocol)
                )
            ).scalar_one_or_none()
            if row is None:
                row = FollowupEnrollmentRow(
                    org_id=org_id, agent_id=agent_id, user_id=user_id,
                    protocol=protocol, start_date=start_date, phase=phase,
                    status="active", enrolled_by=enrolled_by,
                    payer_user_id=payer_user_id, source_ref=source_ref,
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
            await db.execute(
                sa.select(FollowupEnrollmentRow)
                .where(FollowupEnrollmentRow.org_id == org_id)
                .where(FollowupEnrollmentRow.agent_id == agent_id)
                .where(FollowupEnrollmentRow.user_id == user_id)
                .where(FollowupEnrollmentRow.protocol == protocol)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        if start_date is not _UNSET:
            row.start_date = start_date
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


async def protocol_timezones(
    session_factory: Any, *, org_id: uuid.UUID, agent_id: str,
) -> dict[str, str]:
    """Each protocol some active Program of this agent speaks to, with the
    timezone of the first such Program — the zone a person's day is counted in.
    """
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
    zones: dict[str, str] = {}
    for config in configs:
        try:
            audience = Audience.from_config((config or {}).get("audience"))
        except ValueError:
            continue
        if audience is not None:
            zones.setdefault(audience.protocol, str((config or {}).get("timezone") or "UTC"))
    return zones


async def offered_protocols(
    session_factory: Any, *, org_id: uuid.UUID, agent_id: str,
) -> set[str]:
    """Protocols some active Program of this agent speaks to.

    The only protocols a person can be enrolled in: an enrollment no Program
    targets would never produce a single message, and the person would think
    they were being followed.
    """
    return set(await protocol_timezones(session_factory, org_id=org_id, agent_id=agent_id))


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
    checkout — so it can always be told apart from an operator's say-so.  Merged into
    ``platform_meta`` under a row lock: that blob also carries the display
    name, and ops may be writing it at the same moment.  Returns ``False``
    when the person has no identity on that platform.
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
# Settings
# ---------------------------------------------------------------------------


async def get_settings(
    session_factory: Any, *, org_id: uuid.UUID, agent_id: str,
) -> FollowupSettingsRow | None:
    async with session_factory() as db:
        return await db.get(FollowupSettingsRow, (org_id, agent_id))


async def save_settings(
    session_factory: Any,
    *,
    org_id: uuid.UUID,
    agent_id: str,
    escalation_service_account_id: uuid.UUID | None,
    channel_identifier: str | None,
    alert_recipient_user_ids: Iterable[uuid.UUID | str],
    alert_template_name: str | None,
    alert_template_language: str | None,
    message_template_name: str | None,
    message_template_language: str | None,
    welcome_template_name: str | None = None,
    welcome_template_language: str | None = None,
    welcome_text: str | None = None,
    offer_protocols: dict[str, str] | None = None,
    texts: dict[str, str] | None = None,
) -> FollowupSettingsRow:
    recipients = list(dict.fromkeys(str(u) for u in alert_recipient_user_ids))
    async with session_factory() as db:
        row = await db.get(FollowupSettingsRow, (org_id, agent_id))
        if row is None:
            row = FollowupSettingsRow(org_id=org_id, agent_id=agent_id)
            db.add(row)
        row.escalation_service_account_id = escalation_service_account_id
        row.channel_identifier = channel_identifier or None
        row.alert_recipient_user_ids = recipients
        row.alert_template_name = alert_template_name or None
        row.alert_template_language = alert_template_language or None
        row.message_template_name = message_template_name or None
        row.message_template_language = message_template_language or None
        row.welcome_template_name = welcome_template_name or None
        row.welcome_template_language = welcome_template_language or None
        row.welcome_text = (welcome_text or "").strip() or None
        row.offer_protocols = {
            str(k): str(v).strip() for k, v in (offer_protocols or {}).items()
            if str(v).strip()
        }
        row.texts = {
            str(k): str(v).strip() for k, v in (texts or {}).items()
            if str(v).strip()
        }
        row.updated_at = _utcnow()
        await db.commit()
        await db.refresh(row)
        return row


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
    """Remember that this person just wrote to this sender."""
    at = at or _utcnow()
    key = dict(
        org_id=org_id, platform=platform, platform_user_id=platform_user_id,
        channel_identifier=channel_identifier,
    )
    for attempt in (1, 2):
        async with session_factory() as db:
            updated = await db.execute(
                sa.update(ChannelContactRow)
                .where(ChannelContactRow.org_id == org_id)
                .where(ChannelContactRow.platform == platform)
                .where(ChannelContactRow.platform_user_id == platform_user_id)
                .where(ChannelContactRow.channel_identifier == channel_identifier)
                .values(last_inbound_at=at)
            )
            if int(updated.rowcount or 0) == 0:
                db.add(ChannelContactRow(**key, last_inbound_at=at))
            try:
                await db.commit()
                return
            except IntegrityError:
                # Another message from the same person inserted first.
                await db.rollback()
                if attempt == 2:
                    raise


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
    source_session_id: uuid.UUID | None = None,
    created_by: str | None = None,
    dedupe_key: str | None = None,
) -> OutreachMessageRow:
    """Queue one message.  With ``dedupe_key``, queuing the same cause twice
    returns the first row instead of writing to the person again."""
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
            subject_user_id=subject_user_id,
            source_session_id=source_session_id, created_by=created_by,
            dedupe_key=dedupe_key,
        )
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            if dedupe_key is None:
                raise
            existing = (
                await db.execute(
                    sa.select(OutreachMessageRow)
                    .where(OutreachMessageRow.org_id == org_id)
                    .where(OutreachMessageRow.dedupe_key == dedupe_key)
                )
            ).scalar_one()
            return existing
        await db.refresh(row)
        return row
