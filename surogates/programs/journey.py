"""What a Program's ``journey`` asks of the runtime beyond its own cadence.

A journey groups a business's Programs (its steps) under one slug and adds
the settings that span them: the agent's sender on each channel, quiet hours,
a default zone, and when the journey is over.  Inside one, each person is
reached on their own channel and in their own zone.  All of it
is optional in the projection; a Program without a journey behaves as it
always did.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from surogates.channels.capabilities import capabilities_for
from surogates.db.models import (
    ChannelIdentity,
    FollowupEnrollmentRow,
    ProgramInvitationRow,
    ProgramScheduleRow,
)
from surogates.followup import store as followup_store
from surogates.followup.eligibility import Audience, as_utc
from surogates.programs.cadence import next_occurrences

logger = logging.getLogger(__name__)

def journey_of(config: dict) -> dict:
    return (config or {}).get("journey") or {}


def senders_for(config: dict) -> dict[str, str]:
    """The sender per channel this step may write from: the journey's, with
    the step's own channel and sender filling in what it does not name."""
    own = {str(config.get("channel") or "whatsapp"): str(config.get("channel_identifier") or "")}
    merged = {**own, **(journey_of(config).get("senders") or {})}
    return {platform: str(sender) for platform, sender in merged.items() if sender}


def protocol_of(config: dict) -> str | None:
    try:
        audience = Audience.from_config((config or {}).get("audience"))
    except ValueError:
        audience = None
    return audience.protocol if audience else journey_of(config).get("slug")


def person_zone(enrollment: Any, config: dict) -> str:
    """The person's own zone, else the journey's, else the Program's."""
    return str(
        getattr(enrollment, "timezone", None)
        or journey_of(config).get("timezone")
        or (config or {}).get("timezone")
        or "UTC"
    )


def fires_in(config: dict, zone: str, at: datetime) -> bool:
    """Whether the cadence, read in *zone*, has a slot exactly at *at*."""
    return next_occurrences(
        as_utc(at) - timedelta(seconds=1),
        weekdays=config.get("weekdays") or [], times_local=config.get("times_local") or [],
        timezone=zone, count=1,
    ) == [as_utc(at)]


async def zones_of(session_factory: Any, schedule: Any) -> set[str]:
    """Every zone this Program fires in: its people's, or just its own."""
    config = schedule.config or {}
    zones = {person_zone(None, config)}
    if config.get("audience") is None:
        return zones
    protocol = protocol_of(config)
    async with session_factory() as db:
        own = set((
            await db.execute(
                sa.select(sa.distinct(FollowupEnrollmentRow.timezone))
                .where(FollowupEnrollmentRow.org_id == schedule.org_id)
                .where(FollowupEnrollmentRow.agent_id == schedule.agent_id)
                .where(FollowupEnrollmentRow.protocol == protocol)
                .where(FollowupEnrollmentRow.status == "active")
                .where(FollowupEnrollmentRow.timezone.isnot(None))
            )
        ).scalars())
    return zones | own


async def next_run_at(session_factory: Any, schedule: Any, after: datetime) -> datetime | None:
    """The earliest slot after *after* over every zone the Program fires in.

    ponytail: zones are read when a Program fires, so someone enrolled in a
    new zone is first reached after the Program's next slot; recompute on
    enrolment if that one-cadence lag matters.
    """
    config = schedule.config or {}
    slots = [
        found[0]
        for zone in await zones_of(session_factory, schedule)
        if (found := next_occurrences(
            after, weekdays=config.get("weekdays") or [],
            times_local=config.get("times_local") or [], timezone=zone, count=1,
        ))
    ]
    return min(slots, default=None)


def in_quiet_hours(config: dict, zone: str, now: datetime) -> bool:
    quiet = journey_of(config).get("quiet_hours") or {}
    try:
        start = time.fromisoformat(str(quiet["start"]))
        end = time.fromisoformat(str(quiet["end"]))
    except (KeyError, TypeError, ValueError):
        return False
    local = as_utc(now).astimezone(ZoneInfo(zone)).time().replace(tzinfo=None)
    if start <= end:
        return start <= local < end
    return local >= start or local < end


async def _identity(db: Any, org_id: Any, platform: str, user_id: Any) -> Any:
    return (
        await db.execute(
            sa.select(ChannelIdentity)
            .where(ChannelIdentity.org_id == org_id)
            .where(ChannelIdentity.platform == platform)
            .where(ChannelIdentity.user_id == user_id)
            .order_by(ChannelIdentity.id.desc())
            .limit(1)
        )
    ).scalars().first()


async def choose_channel(
    config: dict,
    *,
    org_id: Any,
    user_id: Any,
    enrollment: Any,
    identity_lookup: Any,
    session_factory: Any,
) -> tuple[str, Any, str, list[str]] | None:
    """(platform, identity, sender, the other reachable channels) for one
    person, or ``None`` when no channel with a sender reaches them.

    Reachable means a granted identity there and a sender of the agent's.
    The person's preferred channel wins, then the one they last wrote on,
    then the step's own channel.
    """
    senders = senders_for(config)
    reachable: dict[str, Any] = {}
    for platform in senders:
        if not capabilities_for(platform).can_initiate:
            continue
        identity = await identity_lookup(org_id, platform, user_id)
        if identity is not None and followup_store.permission_status(identity) == "granted":
            reachable[platform] = identity
    if not reachable:
        return None
    chosen = getattr(enrollment, "preferred_channel", None)
    if chosen not in reachable:
        newest = None
        for platform, identity in reachable.items():
            at = await followup_store.last_inbound(
                session_factory, org_id=org_id, platform=platform,
                platform_user_id=identity.platform_user_id, channel_identifier=senders[platform],
            )
            if at is not None and (newest is None or as_utc(at) > newest[0]):
                newest = (as_utc(at), platform)
        own = str(config.get("channel") or "whatsapp")
        chosen = newest[1] if newest else own if own in reachable else next(iter(reachable))
    rest = [p for p in reachable if p != chosen]
    return chosen, reachable[chosen], senders[chosen], rest


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------


async def fall_back(db: Any, row: ProgramInvitationRow, *, now: datetime, config: dict | None = None) -> bool:
    """Move *row* to its next reachable fallback channel, in *db*'s
    transaction.  False when none is left; the row is then untouched."""
    remaining = list(row.fallback_platforms or [])
    if not remaining:
        return False
    if config is None:
        config = (
            await db.execute(
                sa.select(ProgramScheduleRow.config)
                .where(ProgramScheduleRow.program_id == row.program_id)
            )
        ).scalar_one_or_none() or {}
    senders = senders_for(config)
    attempts = list(row.attempts or [])
    attempts.append({
        "platform": row.platform, "state": row.delivery_state,
        "error": row.delivery_error, "at": as_utc(now).isoformat(),
    })
    while remaining:
        platform = remaining.pop(0)
        identity = await _identity(db, row.org_id, platform, row.user_id)
        if (
            platform in senders and identity is not None
            and followup_store.permission_status(identity) == "granted"
        ):
            break
        attempts.append({
            "platform": platform, "state": "skipped",
            "error": "No reachable identity or sender", "at": as_utc(now).isoformat(),
        })
    else:
        row.attempts = attempts
        row.fallback_platforms = None
        return False
    row.attempts = attempts
    row.platform = platform
    row.platform_user_id = identity.platform_user_id
    row.channel_identifier = senders[platform]
    row.fallback_platforms = remaining or None
    row.delivery_state = "queued"
    row.response_state = "not_started"
    row.outbox_id = None
    row.provider_message_id = None
    row.delivery_error = None
    row.deadline_at = None
    row.read_at = None
    return True


async def advance_fallbacks(session_factory: Any, *, now: datetime) -> int:
    """Move every invitation whose send failed to its next channel.

    The dispatcher's failures move at once (see :mod:`surogates.programs.delivery`);
    this catches the ones the send pass itself could not hand over.
    """
    moved = 0
    async with session_factory() as db:
        rows = (
            await db.execute(
                sa.select(ProgramInvitationRow)
                .where(ProgramInvitationRow.fallback_platforms.isnot(None))
                .where(ProgramInvitationRow.delivery_state.in_(("failed", "undelivered")))
                .where(ProgramInvitationRow.response_state.in_(("not_started", "awaiting_reply")))
                .limit(200)
            )
        ).scalars().all()
        for row in rows:
            if row.fallback_platforms and await fall_back(db, row, now=now):
                moved += 1
        await db.commit()
    return moved


# ---------------------------------------------------------------------------
# Exit
# ---------------------------------------------------------------------------


async def finish_journeys(session_factory: Any, *, now: datetime) -> int:
    """End every active enrollment past its journey's ``exit_after_days``."""
    async with session_factory() as db:
        rows = (
            await db.execute(
                sa.select(ProgramScheduleRow.org_id, ProgramScheduleRow.agent_id, ProgramScheduleRow.config)
                .where(ProgramScheduleRow.active.is_(True))
            )
        ).all()
    journeys: dict[tuple, tuple[int, str]] = {}
    for org_id, agent_id, config in rows:
        days = journey_of(config).get("exit_after_days")
        protocol = protocol_of(config or {})
        if isinstance(days, int) and not isinstance(days, bool) and days > 0 and protocol:
            journeys.setdefault((org_id, agent_id, protocol), (days, person_zone(None, config)))
    finished = 0
    for (org_id, agent_id, protocol), (days, zone) in journeys.items():
        last_start = as_utc(now).astimezone(ZoneInfo(zone)).date() - timedelta(days=days)
        async with session_factory() as db:
            result = await db.execute(
                sa.update(FollowupEnrollmentRow)
                .where(FollowupEnrollmentRow.org_id == org_id)
                .where(FollowupEnrollmentRow.agent_id == agent_id)
                .where(FollowupEnrollmentRow.protocol == protocol)
                .where(FollowupEnrollmentRow.status == "active")
                .where(FollowupEnrollmentRow.start_date.isnot(None))
                .where(FollowupEnrollmentRow.start_date < last_start)
                .values(status="ended", status_reason="Journey finished", updated_at=now)
            )
            await db.commit()
        finished += int(result.rowcount or 0)
    return finished
