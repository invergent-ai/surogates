"""Turn one due instant into an occurrence and one invitation per user.

Every user on the roster gets a row whatever the outcome, and a skip is
recorded on that row rather than dropping it.  The roster is the denominator
run history counts against: dropping an unreachable user would quietly turn
"12 of 14" into "12 of 13", and the two unreachable people would vanish from
the record instead of being the thing the operator needs to see.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from surogates.db.models import (
    ChannelIdentity,
    ProgramInvitationRow,
    ProgramOccurrenceRow,
    ProgramScheduleRow,
)

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


def platform_of(identity: Any, schedule: Any) -> str:
    """The channel this invitation is on — the identity's if it says, else the Program's."""
    return (
        getattr(identity, "platform", None)
        or (schedule.config or {}).get("channel")
        or "whatsapp"
    )


def _permission(identity: Any) -> str:
    meta = getattr(identity, "platform_meta", None) or {}
    return (meta.get("contact_permission") or {}).get("status") or "none"


async def _existing_occurrence(db, program_id, scheduled_for) -> uuid.UUID:
    """The occurrence a racing tick already created for this instant."""
    return (
        await db.execute(
            sa.select(ProgramOccurrenceRow.id)
            .where(ProgramOccurrenceRow.program_id == program_id)
            .where(ProgramOccurrenceRow.scheduled_for == scheduled_for)
        )
    ).scalar_one()


async def _has_open_invitation(db, schedule, identity) -> bool:
    """Is this user already mid-check-in with this agent?

    Counts an unanswered opener too.  The sweep runs first in the tick, so an
    ``awaiting_reply`` row still here is one whose deadline has not passed —
    a live question the user has yet to answer, and not something to talk
    over with a second one.
    """
    return (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(ProgramInvitationRow)
            # org_id leads the open-invitations index; without it this is a
            # sequential scan of an append-only table, once per user per
            # occurrence, inside the claim lease.
            .where(ProgramInvitationRow.org_id == schedule.org_id)
            .where(ProgramInvitationRow.agent_id == schedule.agent_id)
            .where(ProgramInvitationRow.platform == platform_of(identity, schedule))
            .where(
                ProgramInvitationRow.platform_user_id
                == identity.platform_user_id
            )
            .where(
                ProgramInvitationRow.response_state.in_(_OPEN_RESPONSE_STATES)
            )
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
    """
    config = schedule.config or {}
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

        platform = config.get("channel", "whatsapp")
        for user_id in config.get("users", []):
            reason: str | None = None
            state = "queued"
            identity = await identity_lookup(
                schedule.org_id, platform, uuid.UUID(user_id),
            )
            if identity is None:
                reason = f"No {platform} identity"
                state = "skipped"
            elif _permission(identity) != "granted":
                reason = "No permission to message"
                state = "skipped"
            elif await _has_open_invitation(db, schedule, identity):
                reason = "Previous check-in still open"
                state = "skipped"

            db.add(
                ProgramInvitationRow(
                    occurrence_id=occ.id,
                    program_id=schedule.program_id,
                    org_id=schedule.org_id,
                    agent_id=schedule.agent_id,
                    user_id=uuid.UUID(user_id),
                    platform=platform,
                    platform_user_id=(
                        identity.platform_user_id if identity else ""
                    ),
                    delivery_state=state,
                    response_state="not_started",
                    escalation_state="none",
                    reason=reason,
                )
            )
        await db.commit()
        return occ.id


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
        deadlines = await _deadline_hours_by_program(
            db, {row.program_id for row, _ in rows},
        )

    for pending, scheduled_for in rows:
        window = timedelta(
            hours=deadlines.get(pending.program_id, _DEFAULT_DEADLINE_HOURS),
        )
        async with session_factory() as db:
            if scheduled_for is not None and _as_utc(scheduled_for) + window <= now:
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

            if identity is None or _permission(identity) != "granted":
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


async def _release(db: Any, invitation_id: Any) -> None:
    """Undo a claim whose enqueue handed nothing over."""
    await db.execute(
        sa.update(ProgramInvitationRow)
        .where(ProgramInvitationRow.id == invitation_id)
        .where(ProgramInvitationRow.outbox_id.is_(None))
        .values(response_state="not_started", deadline_at=None)
    )
    await db.commit()


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _deadline_hours_by_program(db, program_ids: set) -> dict:
    """Each Program's response deadline, read from its projected config."""
    if not program_ids:
        return {}
    rows = (
        await db.execute(
            sa.select(ProgramScheduleRow.program_id, ProgramScheduleRow.config)
            .where(ProgramScheduleRow.program_id.in_(program_ids))
        )
    ).all()
    return {
        program_id: int(
            (config or {}).get("response_deadline_hours")
            or _DEFAULT_DEADLINE_HOURS
        )
        for program_id, config in rows
    }


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
