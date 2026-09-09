"""Turn one due instant into an occurrence and one invitation per patient.

Every patient on the roster gets a row whatever the outcome, and a skip is
recorded on that row rather than dropping it.  The roster is the denominator
run history counts against: dropping an unreachable patient would quietly turn
"12 of 14" into "12 of 13", and the two unreachable people would vanish from
the record instead of being the thing the operator needs to see.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from surogates.db.models import (
    ChannelIdentity,
    ProgramInvitationRow,
    ProgramOccurrenceRow,
)

#: Response states meaning "this person is mid-check-in".  A patient in one of
#: these must not be handed a second opener.
_OPEN_RESPONSE_STATES = ("replied", "in_progress")


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

    Deliberately does not filter on ``awaiting_reply``: an unanswered opener is
    closed by the deadline sweep before the next tick evaluates the roster, so
    by the time this runs an ``awaiting_reply`` row is either expired or from
    this same occurrence.
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
