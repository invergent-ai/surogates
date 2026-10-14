"""Turn one due instant into an occurrence and one invitation per user.

Every user on the roster gets a row whatever the outcome, and a skip is
recorded on that row rather than dropping it.  The roster is the denominator
run history counts against: dropping an unreachable user would quietly turn
"12 of 14" into "12 of 13", and the two unreachable people would vanish from
the record instead of being the thing the operator needs to see.

A Program with an ``audience`` has no fixed roster: its roster for one
occurrence is every active enrollment of the audience's protocol that is due
today (its day window, phase and silence filter).  People who are simply not
due — someone on day 40 for a first-month Program — get no row at all: they
were never part of this occurrence, unlike an unreachable person who was.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from surogates.db.models import (
    ChannelIdentity,
    ProgramInvitationRow,
    ProgramOccurrenceRow,
    ProgramScheduleRow,
)
from surogates.followup import store as followup_store
from surogates.followup.eligibility import Audience, as_utc, ineligibility
from surogates.followup.missed import paused_by_silence, threshold
from surogates.programs.journey import (
    choose_channel,
    fires_in,
    in_quiet_hours,
    journey_of,
    person_zone,
    protocol_of,
)
from surogates.programs.opener import OpenerUndeliverable

logger = logging.getLogger(__name__)

#: Response states meaning "this person is mid-check-in".  A user in one of
#: these must not be handed a second opener.
#
#: ``awaiting_reply`` belongs here.  The canonical cadence is twice a day and
#: the default response deadline is a full day, so the next occurrence arrives
#: while the first opener is still live: without it the user is asked two
#: questions at once and whichever they do not answer is later swept as a
#: non-response.  The deadline sweep is what eventually clears these — it runs
#: first in the tick precisely so an expired opener does not suppress the next
#: check-in.
_OPEN_RESPONSE_STATES = ("awaiting_reply", "replied", "in_progress")

#: Used when a Program's projected config carries no deadline of its own.
_DEFAULT_DEADLINE_HOURS = 24


async def _existing_occurrence(db, program_id, scheduled_for) -> uuid.UUID:
    """The occurrence a racing tick already created for this instant."""
    return (
        await db.execute(
            sa.select(ProgramOccurrenceRow.id)
            .where(ProgramOccurrenceRow.program_id == program_id)
            .where(ProgramOccurrenceRow.scheduled_for == scheduled_for)
        )
    ).scalar_one()


async def _has_open_invitation(db, schedule, user_id) -> bool:
    """Is this user already in the middle of a step with this agent, on any channel?

    Counts an unanswered opener too.  The sweep runs first in the tick, so an
    ``awaiting_reply`` row still here is one whose deadline has not passed —
    a live question the user has yet to answer, and not something to talk
    over with a second one.
    """
    return (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(ProgramInvitationRow)
            # The person, not one of their channels: a question still open
            # on WhatsApp must not be asked again on Telegram.  Served by
            # idx_program_invitations_person_open.
            .where(ProgramInvitationRow.org_id == schedule.org_id)
            .where(ProgramInvitationRow.agent_id == schedule.agent_id)
            .where(ProgramInvitationRow.user_id == user_id)
            # A queued opener not yet sent (held through quiet hours, or
            # from a step that fired in the same tick) is a question too.
            .where(sa.or_(
                ProgramInvitationRow.response_state.in_(_OPEN_RESPONSE_STATES),
                sa.and_(
                    ProgramInvitationRow.delivery_state == "queued",
                    ProgramInvitationRow.response_state == "not_started",
                ),
            ))
        )
    ).scalar_one() > 0


async def materialize_occurrence(
    schedule: Any,
    *,
    session_factory: Any,
    identity_lookup: Any,
    now: datetime,
) -> uuid.UUID:
    """Create the occurrence and one invitation per user.

    Idempotent at both levels — ``(program_id, scheduled_for)`` and
    ``(occurrence_id, user_id)`` — so a crashed or retried tick is harmless
    rather than a second message to every user.

    Inside a journey each person is reached on their own channel (see
    :func:`choose_channel`) and asked only in the zone the slot is for.
    """
    config = schedule.config or {}
    platform = config.get("channel", "whatsapp")
    per_person = bool(journey_of(config))
    pause = threshold(config, "pause_after_missed")
    # Resolved before the occurrence's transaction opens: an audience is one
    # read per enrolled person, the pause one per reachable person, and none
    # of it needs the row lock.
    roster, last_by_user = await _roster(
        schedule, config, platform=platform, now=now,
        session_factory=session_factory, identity_lookup=identity_lookup,
        with_last_inbound=pause is not None,
    )
    chosen: dict[uuid.UUID, Any] = {}
    if per_person:
        for user_id, _identity, enrollment in roster:
            chosen[user_id] = await choose_channel(
                config, org_id=schedule.org_id, user_id=user_id,
                enrollment=enrollment, identity_lookup=identity_lookup,
                session_factory=session_factory,
            )
    paused: set[uuid.UUID] = set()
    if pause:
        for user_id, identity, _enrollment in roster:
            if per_person and chosen[user_id] is not None:
                identity = chosen[user_id][1]
            if (
                identity is not None
                and followup_store.permission_status(identity) == "granted"
                and await paused_by_silence(
                    session_factory, program_id=schedule.program_id,
                    user_id=user_id, count=pause,
                    last_inbound_at=last_by_user.get(user_id),
                )
            ):
                paused.add(user_id)
    async with session_factory() as db:
        occ = ProgramOccurrenceRow(
            program_id=schedule.program_id,
            org_id=schedule.org_id,
            agent_id=schedule.agent_id,
            scheduled_for=now,
            skill_ref=config.get("skill_ref", ""),
            template_name=config.get("template_name"),
            template_language=config.get("template_language"),
            status="fired",
        )
        db.add(occ)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            return await _existing_occurrence(db, schedule.program_id, now)

        for user_id, identity, _enrollment in roster:
            reason: str | None = None
            state = "queued"
            row_platform, sender, fallbacks = platform, config.get("channel_identifier"), []
            if per_person and chosen[user_id] is not None:
                row_platform, identity, sender, fallbacks = chosen[user_id]
            elif identity is None:
                reason = f"No {platform} identity"
            elif followup_store.permission_status(identity) != "granted":
                reason = "No permission to message"
            elif per_person:
                reason = "No reachable channel"  # reachable, but no sender for it
            if reason is not None:
                pass
            elif await _has_open_invitation(db, schedule, user_id):
                reason = "An earlier step is still waiting for an answer"
            elif user_id in paused:
                reason = (
                    f"Paused: the last {pause} openers went unanswered; "
                    "resumes when they write"
                )
            if reason is not None:
                state = "skipped"

            db.add(
                ProgramInvitationRow(
                    occurrence_id=occ.id,
                    program_id=schedule.program_id,
                    org_id=schedule.org_id,
                    agent_id=schedule.agent_id,
                    user_id=user_id,
                    platform=row_platform,
                    platform_user_id=(
                        identity.platform_user_id if identity else ""
                    ),
                    channel_identifier=sender or None,
                    fallback_platforms=fallbacks or None,
                    delivery_state=state,
                    response_state="not_started",
                    escalation_state="none",
                    reason=reason,
                )
            )
        await db.commit()
        return occ.id


async def _roster(
    schedule: Any,
    config: dict,
    *,
    platform: str,
    now: datetime,
    session_factory: Any,
    identity_lookup: Any,
    with_last_inbound: bool = False,
) -> tuple[list[tuple[uuid.UUID, Any, Any]], dict[uuid.UUID, datetime]]:
    """The people this occurrence is for, each with their channel identity
    and enrollment (``None`` on a fixed roster).

    A fixed roster is returned whole.  An audience is resolved against the
    enrollments as they stand right now, filtered to who is due today in
    their own zone.  When people have zones of their own, only those whose
    zone produced this slot are asked: the same cadence fires once per zone.

    Also returns when each person last wrote, by user: to the Program's
    sender, or inside a journey to any of the business's channels (a person
    may be reached on another).  Read in one query when the audience's
    silence filter or the caller (*with_last_inbound*, for the pause) needs
    it, else empty.
    """
    sender = str(config.get("channel_identifier") or "")

    async def _last_inbound(identities: dict) -> dict[uuid.UUID, datetime]:
        if journey_of(config):
            return await followup_store.last_inbound_by_person(
                session_factory, org_id=schedule.org_id, user_ids=list(identities),
            )
        by_phone = await followup_store.last_inbound_by_user(
            session_factory, org_id=schedule.org_id, platform=platform,
            channel_identifier=sender,
            platform_user_ids=[i.platform_user_id for i in identities.values() if i is not None],
        )
        return {
            uid: by_phone[i.platform_user_id]
            for uid, i in identities.items() if i is not None and i.platform_user_id in by_phone
        }

    if config.get("audience") is None:
        roster = []
        for user_id in config.get("users", []):
            uid = uuid.UUID(user_id)
            roster.append(
                (uid, await identity_lookup(schedule.org_id, platform, uid), None),
            )
        last = await _last_inbound({u: i for u, i, _ in roster}) if with_last_inbound else {}
        return roster, last

    try:
        audience = Audience.from_config(config.get("audience"))
    except ValueError as exc:
        # Ops validates audiences before projecting them, so this is drift.
        # Messaging nobody is the only safe reading of a filter we cannot
        # understand.
        logger.warning(
            "[programs] program %s has an unreadable audience (%s); "
            "nobody is messaged", schedule.program_id, exc,
        )
        return [], {}

    enrollments = await followup_store.enrollments(
        session_factory, org_id=schedule.org_id, agent_id=schedule.agent_id,
        protocol=audience.protocol, status="active",
    )
    zones = {e.user_id: person_zone(e, config) for e in enrollments}
    if set(zones.values()) - {str(config.get("timezone") or "UTC")}:
        enrollments = [e for e in enrollments if fires_in(config, zones[e.user_id], now)]
    identities = {
        e.user_id: await identity_lookup(schedule.org_id, platform, e.user_id)
        for e in enrollments
    }
    last_by_user: dict[uuid.UUID, datetime] = {}
    if audience.silent_days is not None or with_last_inbound:
        last_by_user = await _last_inbound(identities)
    roster = []
    for enrollment in enrollments:
        last = last_by_user.get(enrollment.user_id)
        if ineligibility(
            audience,
            start_date=enrollment.start_date,
            phase=enrollment.phase,
            enrolled_at=enrollment.created_at,
            last_inbound_at=last,
            today=now.astimezone(ZoneInfo(zones[enrollment.user_id])).date(),
            now=now,
        ) is None:
            roster.append((enrollment.user_id, identities[enrollment.user_id], enrollment))
    return roster, last_by_user


async def send_queued_openers(
    session_factory: Any,
    *,
    identity_lookup: Any,
    now: datetime,
    enqueue: Any = None,
) -> int:
    """Re-check permission on every queued opener, then hand on the survivors.

    The permission re-check is not belt-and-braces.  It is the only mechanism
    by which a withdrawal reaches an opener that is already queued — ops never
    calls into the runtime to cancel anything — and it closes the race where
    permission is withdrawn between materialising the occurrence and sending.

    *enqueue* is ``async (invitation) -> int | None`` returning the **outbox
    row id** — never a provider message id, which does not exist until the
    dispatcher has posted.  The invitation is keyed on that row and the
    dispatcher reports the provider id back through it.  It is optional so
    the cancellation pass can be exercised alone; production always passes
    it, and the ticker refuses to start without one.

    Every row is committed on its own.  A single commit at the end meant a
    failure on the k-th enqueue rolled back the first k-1, whose openers
    were already in the outbox — and the next tick sent them all again.

    Each row is **claimed before** it is handed over: a conditional update
    flips it to ``awaiting_reply`` and only the worker whose update took
    effect calls *enqueue*.  The outbox row is committed inside *enqueue* and
    the invitation learns its id in a later commit; a process that dies in
    between used to leave a row that looked untouched, and the next tick sent
    the user a second approved template.  Now it leaves a claimed row with
    no outbox id, which the deadline sweep records as a failed delivery — one
    user missed and visible, rather than one user messaged twice.  The
    same claim is what keeps two replicas from sending the same opener when
    the leader lease lapses mid-pass.

    Returns the number of invitations cancelled.
    """
    cancelled = 0
    async with session_factory() as db:
        rows = (
            await db.execute(
                sa.select(ProgramInvitationRow, ProgramOccurrenceRow.scheduled_for)
                .join(
                    ProgramOccurrenceRow,
                    ProgramOccurrenceRow.id == ProgramInvitationRow.occurrence_id,
                )
                .where(ProgramInvitationRow.delivery_state == "queued")
                .where(ProgramInvitationRow.outbox_id.is_(None))
                .where(ProgramInvitationRow.response_state == "not_started")
                .order_by(ProgramInvitationRow.created_at)
            )
        ).all()
        configs = await _configs_by_program(db, {row.program_id for row, _ in rows})

    for pending, scheduled_for in rows:
        config = configs.get(pending.program_id) or {}
        window = timedelta(
            hours=int(config.get("response_deadline_hours") or _DEFAULT_DEADLINE_HOURS),
        )
        if journey_of(config).get("quiet_hours") and in_quiet_hours(
            config, await _zone_of(session_factory, pending, config), now,
        ):
            continue  # held until the person's quiet hours are over
        async with session_factory() as db:
            if scheduled_for is not None and as_utc(scheduled_for) + window <= now:
                # The slot's own response window has already closed.  A
                # "how are you this morning" sent two days late is wrong,
                # and retrying it every tick forever is worse.
                await db.execute(_unsent(pending.id).values(
                    delivery_state="skipped",
                    reason="Opener not sent within the response window",
                ))
                await db.commit()
                continue

            try:
                identity = await identity_lookup(
                    pending.org_id, pending.platform, pending.user_id,
                )
            except Exception:  # noqa: BLE001 — one user, not the fleet
                logger.exception(
                    "[programs] identity lookup failed for invitation %s; "
                    "leaving it queued", pending.id,
                )
                continue

            if identity is None or followup_store.permission_status(identity) != "granted":
                withdrawn = await db.execute(_unsent(pending.id).values(
                    delivery_state="canceled",
                    reason="Permission withdrawn before the opener was sent",
                ))
                cancelled += int(withdrawn.rowcount or 0)
                await db.commit()
                continue

            if enqueue is None:
                continue

            claimed = await db.execute(_unsent(pending.id).values(
                response_state="awaiting_reply", deadline_at=now + window,
            ))
            await db.commit()
            if int(claimed.rowcount or 0) != 1:
                continue  # another worker got here first

            try:
                outbox_id = await enqueue(pending)
            except OpenerUndeliverable as exc:
                # Retrying will not change the answer; say so on the row and
                # free the user for the next occurrence.
                await _fail(db, pending.id, exc.reason)
                continue
            except Exception:
                await _release(db, pending.id)
                raise
            if outbox_id is None:
                # Nothing was handed over; give it back so the next tick
                # retries rather than starting a deadline nobody was asked
                # to meet.
                await _release(db, pending.id)
                continue
            await db.execute(
                sa.update(ProgramInvitationRow)
                .where(ProgramInvitationRow.id == pending.id)
                .values(outbox_id=int(outbox_id))
            )
            await db.commit()

    return cancelled


def _unsent(invitation_id: Any):
    """An UPDATE that only takes effect while nobody has touched the row."""
    return (
        sa.update(ProgramInvitationRow)
        .where(ProgramInvitationRow.id == invitation_id)
        .where(ProgramInvitationRow.delivery_state == "queued")
        .where(ProgramInvitationRow.outbox_id.is_(None))
        .where(ProgramInvitationRow.response_state == "not_started")
    )


async def _fail(db: Any, invitation_id: Any, reason: str) -> None:
    """A claimed opener that can never be handed over: failed, and not awaited."""
    await db.execute(
        sa.update(ProgramInvitationRow)
        .where(ProgramInvitationRow.id == invitation_id)
        .where(ProgramInvitationRow.outbox_id.is_(None))
        .values(
            delivery_state="failed",
            delivery_error=reason[:500],
            response_state="not_started",
            deadline_at=None,
        )
    )
    await db.commit()


async def _release(db: Any, invitation_id: Any) -> None:
    """Undo a claim whose enqueue handed nothing over."""
    await db.execute(
        sa.update(ProgramInvitationRow)
        .where(ProgramInvitationRow.id == invitation_id)
        .where(ProgramInvitationRow.outbox_id.is_(None))
        .values(response_state="not_started", deadline_at=None)
    )
    await db.commit()


async def _configs_by_program(db, program_ids: set) -> dict:
    """Each Program's projected config."""
    if not program_ids:
        return {}
    rows = (
        await db.execute(
            sa.select(ProgramScheduleRow.program_id, ProgramScheduleRow.config)
            .where(ProgramScheduleRow.program_id.in_(program_ids))
        )
    ).all()
    return {program_id: config or {} for program_id, config in rows}


async def _zone_of(session_factory: Any, invitation: Any, config: dict) -> str:
    """The zone *invitation*'s person lives by in this Program's journey."""
    found = await followup_store.enrollments(
        session_factory, org_id=invitation.org_id, agent_id=invitation.agent_id,
        user_id=invitation.user_id, protocol=protocol_of(config),
    )
    return person_zone(found[0] if found else None, config)


def identity_lookup_from(session_factory: Any):
    """The production identity lookup: the user's ``channel_identities`` row."""

    async def _lookup(org_id, platform, user_id):
        async with session_factory() as db:
            # Newest first, and never scalar_one: nothing unique guards
            # (org, platform, user), so a user whose new number was linked
            # without removing the old one has two rows, and raising here
            # would stop every opener in the fleet on this user's account.
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

    return _lookup
