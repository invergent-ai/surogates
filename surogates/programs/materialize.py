"""Turn one due instant into an occurrence and one invitation per patient.

Every patient on the roster gets a row whatever the outcome, and a skip is
recorded on that row rather than dropping it.  The roster is the denominator
run history counts against: dropping an unreachable patient would quietly turn
"12 of 14" into "12 of 13", and the two unreachable people would vanish from
the record instead of being the thing the operator needs to see.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from surogates.db.models import (
    ChannelIdentity,
    ProgramInvitationRow,
    ProgramOccurrenceRow,
    ProgramScheduleRow,
)

#: Response states meaning "this person is mid-check-in".  A patient in one of
#: these must not be handed a second opener.
#
#: ``awaiting_reply`` belongs here.  The canonical cadence is twice a day and
#: the default response deadline is a full day, so the next occurrence arrives
#: while the first opener is still live: without it the patient is asked two
#: questions at once and whichever they do not answer is later swept as a
#: non-response.  The deadline sweep is what eventually clears these — it runs
#: first in the tick precisely so an expired opener does not suppress the next
#: check-in.
_OPEN_RESPONSE_STATES = ("awaiting_reply", "replied", "in_progress")

#: Used when a Program's projected config carries no deadline of its own.
_DEFAULT_DEADLINE_HOURS = 24


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
    """Is this patient already mid-check-in with this agent?

    Counts an unanswered opener too.  The sweep runs first in the tick, so an
    ``awaiting_reply`` row still here is one whose deadline has not passed —
    a live question the patient has yet to answer, and not something to talk
    over with a second one.
    """
    return (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(ProgramInvitationRow)
            .where(ProgramInvitationRow.agent_id == schedule.agent_id)
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
    """Create the occurrence and one invitation per patient.

    Idempotent at both levels — ``(program_id, scheduled_for)`` and
    ``(occurrence_id, user_id)`` — so a crashed or retried tick is harmless
    rather than a second message to every patient.
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
        for user_id in config.get("patients", []):
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

    *enqueue* is ``async (invitation) -> str | None`` returning the provider
    message id.  It is optional so the cancellation pass can be exercised on
    its own; when it is absent, survivors stay queued for the next tick rather
    than being silently marked sent.

    Returns the number of invitations cancelled.
    """
    cancelled = 0
    async with session_factory() as db:
        rows = (
            await db.execute(
                sa.select(ProgramInvitationRow)
                .where(ProgramInvitationRow.delivery_state == "queued")
                .order_by(ProgramInvitationRow.created_at)
            )
        ).scalars().all()

        survivors = []
        for row in rows:
            identity = await identity_lookup(
                row.org_id, row.platform, row.user_id,
            )
            if identity is None or _permission(identity) != "granted":
                row.delivery_state = "canceled"
                row.reason = "Permission withdrawn before the opener was sent"
                cancelled += 1
                continue
            survivors.append(row)
        await db.commit()

        if enqueue is None:
            return cancelled

        deadlines = await _deadline_hours_by_program(
            db, {row.program_id for row in survivors},
        )
        for row in survivors:
            provider_message_id = await enqueue(row)
            if provider_message_id is None:
                # The opener was not accepted; leave it queued so the next
                # tick retries rather than starting a deadline nobody was
                # asked to meet.
                continue
            row.provider_message_id = provider_message_id
            row.delivery_state = "accepted"
            row.response_state = "awaiting_reply"
            row.deadline_at = now + timedelta(
                hours=deadlines.get(row.program_id, _DEFAULT_DEADLINE_HOURS),
            )
        await db.commit()

    return cancelled


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
    """The production identity lookup: the patient's ``channel_identities`` row."""

    async def _lookup(org_id, platform, user_id):
        async with session_factory() as db:
            return (
                await db.execute(
                    sa.select(ChannelIdentity)
                    .where(ChannelIdentity.org_id == org_id)
                    .where(ChannelIdentity.platform == platform)
                    .where(ChannelIdentity.user_id == user_id)
                )
            ).scalar_one_or_none()

    return _lookup
