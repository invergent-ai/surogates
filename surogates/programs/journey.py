"""What a Program's ``journey`` asks of the runtime beyond its own cadence.

A journey groups a business's Programs (its steps) under one slug and adds
the settings that span them: the agent's sender on each channel, quiet hours,
and when the journey is over.  Inside one, each person is
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
    ChannelContactRow,
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
    """The sender per channel this step may write from: the step's own on
    its channel (the one Studio checks before activating and test-sends
    from), the journey's on every other."""
    own = {str(config.get("channel") or "whatsapp"): str(config.get("channel_identifier") or "")}
    merged = {**(journey_of(config).get("senders") or {}), **{p: s for p, s in own.items() if s}}
    return {platform: str(sender) for platform, sender in merged.items() if sender}


def protocol_of(config: dict) -> str | None:
    try:
        audience = Audience.from_config((config or {}).get("audience"))
    except ValueError:
        audience = None
    return audience.protocol if audience else journey_of(config).get("slug")


def person_zone(enrollment: Any, config: dict) -> str:
    """The person's own zone, else the Program's.

    Never the journey's: that is only Studio's default for new steps, and a
    step's slots are computed in its own zone everywhere else (reconcile,
    the store), so reading the journey's here would match none of them.
    """
    return str(getattr(enrollment, "timezone", None) or (config or {}).get("timezone") or "UTC")


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

    Zones that appear later (someone enrolled in a new zone, or a person's
    zone changed) are caught by :func:`pull_forward_for_new_zones`.
    """
    return _earliest(schedule.config or {}, await zones_of(session_factory, schedule), after)


def _earliest(config: dict, zones: set[str], after: datetime) -> datetime | None:
    slots = [
        found[0]
        for zone in zones
        if (found := next_occurrences(
            after, weekdays=config.get("weekdays") or [],
            times_local=config.get("times_local") or [], timezone=zone, count=1,
        ))
    ]
    return min(slots, default=None)


async def pull_forward_for_new_zones(session_factory: Any, *, now: datetime) -> int:
    """Bring each audience Program's ``next_run_at`` forward when a zone
    among its enrolled people now has an earlier slot.

    ``next_run_at`` is computed when a Program fires, from the zones it had
    then; a person enrolled in a new zone, or whose zone changed, would wait
    a whole cadence for their first message while Studio showed a sooner
    one.  Never moves a Program later, and never touches a leased row.  Two
    reads per pass, whatever the fleet's size.
    """
    async with session_factory() as db:
        schedules = (
            await db.execute(
                sa.select(
                    ProgramScheduleRow.program_id, ProgramScheduleRow.org_id,
                    ProgramScheduleRow.agent_id, ProgramScheduleRow.config,
                    ProgramScheduleRow.next_run_at,
                )
                .where(ProgramScheduleRow.active.is_(True))
                .where(sa.or_(
                    ProgramScheduleRow.locked_until.is_(None),
                    ProgramScheduleRow.locked_until <= now,
                ))
            )
        ).all()
        zones: dict[tuple, set[str]] = {}
        for org_id, agent_id, protocol, zone in (
            await db.execute(
                sa.select(
                    FollowupEnrollmentRow.org_id, FollowupEnrollmentRow.agent_id,
                    FollowupEnrollmentRow.protocol, FollowupEnrollmentRow.timezone,
                )
                .where(FollowupEnrollmentRow.status == "active")
                .where(FollowupEnrollmentRow.timezone.isnot(None))
                .distinct()
            )
        ).all():
            zones.setdefault((org_id, agent_id, protocol), set()).add(zone)
    moved = 0
    for program_id, org_id, agent_id, config, current in schedules:
        config = config or {}
        if config.get("audience") is None:
            continue
        own = zones.get((org_id, agent_id, protocol_of(config)))
        if not own:
            continue
        try:
            earliest = _earliest(config, own | {person_zone(None, config)}, now)
        except Exception:  # noqa: BLE001 — a bad zone costs this Program, not the pass
            logger.warning("[programs] could not read the zones of program %s", program_id)
            continue
        if earliest is None or (current is not None and as_utc(current) <= earliest):
            continue
        async with session_factory() as db:
            result = await db.execute(
                sa.update(ProgramScheduleRow)
                .where(ProgramScheduleRow.program_id == program_id)
                .where(
                    ProgramScheduleRow.next_run_at.is_(None) if current is None
                    else ProgramScheduleRow.next_run_at == current
                )
                .values(next_run_at=earliest)
            )
            await db.commit()
        moved += int(result.rowcount or 0)
    return moved


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


def quiet_end(config: dict, zone: str, now: datetime) -> datetime:
    """When the quiet hours *now* falls in end, in UTC."""
    end = time.fromisoformat(str(journey_of(config)["quiet_hours"]["end"]))
    local = as_utc(now).astimezone(ZoneInfo(zone))
    at = datetime.combine(local.date(), end, tzinfo=ZoneInfo(zone))
    if at <= local:
        at += timedelta(days=1)
    return as_utc(at.astimezone(ZoneInfo("UTC")))


async def quiet_until(
    session_factory: Any, *, org_id: Any, agent_id: str, user_id: Any, now: datetime,
) -> tuple[datetime, str] | None:
    """``(when, zone)`` a message to this person may go out, when they are
    in the quiet hours of a journey they are active in; else ``None``.

    The latest end wins when two journeys disagree.
    """
    by_protocol = {}
    for config in await followup_store.active_program_configs(
        session_factory, org_id=org_id, agent_id=agent_id,
    ):
        protocol = protocol_of(config)
        if protocol and journey_of(config).get("quiet_hours"):
            by_protocol.setdefault(protocol, config)
    if not by_protocol:
        return None
    held = None
    for enrollment in await followup_store.enrollments(
        session_factory, org_id=org_id, agent_id=agent_id, user_id=user_id, status="active",
    ):
        config = by_protocol.get(enrollment.protocol)
        if config is None:
            continue
        zone = person_zone(enrollment, config)
        if in_quiet_hours(config, zone, now):
            end = quiet_end(config, zone, now)
            if held is None or end > held[0]:
                held = (end, zone)
    return held


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
    agent_id: str | None = None,
) -> tuple[str, Any, str, list[str]] | None:
    """(platform, identity, sender, the other reachable channels) for one
    person, or ``None`` when no channel with a sender reaches them.

    Reachable means a granted identity there and a sender of the agent's,
    and, on a channel where the platform can only answer someone who wrote
    first (Telegram), that the person has written to that sender.  The
    person's preferred channel wins, then the one they last wrote on, then
    the step's own channel.
    """
    senders = senders_for(config)
    reachable: dict[str, Any] = {}
    last: dict[str, datetime | None] = {}
    for platform in senders:
        caps = capabilities_for(platform)
        if not caps.can_initiate:
            continue
        identity = await identity_lookup(org_id, platform, user_id)
        if identity is None or followup_store.permission_status(identity) != "granted":
            continue
        last[platform] = await followup_store.last_inbound(
            session_factory, org_id=org_id, platform=platform,
            platform_user_id=identity.platform_user_id, channel_identifier=senders[platform],
        )
        if caps.needs_prior_contact and last[platform] is None:
            continue
        reachable[platform] = identity
    if not reachable:
        return None
    chosen = getattr(enrollment, "preferred_channel", None)
    if enrollment is None and agent_id is not None:
        # Outside one journey (a fixed roster, a contact's copy): the channel
        # the person most recently asked for in any of theirs.
        chosen = followup_store.preferred_channel(await followup_store.enrollments(
            session_factory, org_id=org_id, agent_id=agent_id, user_id=user_id,
        ))
    if chosen not in reachable:
        newest = None
        for platform in reachable:
            at = last[platform]
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
            and (
                not capabilities_for(platform).needs_prior_contact
                or await db.get(ChannelContactRow, (
                    row.org_id, platform, identity.platform_user_id, senders[platform],
                )) is not None
            )
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
