"""Recognise an inbound message as the reply to a check-in.

The match is on ``(org_id, agent_id, platform, platform_user_id)`` and an open
response state.  ``agent_id`` is part of the key because ``channel_identities``
is org-scoped and one person can be bound to several agents: without it, one
agent's Program would swallow another agent's reply.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa

from surogates.db.models import (
    FollowupContactRow,
    ProgramInvitationRow,
    ProgramOccurrenceRow,
    User,
)

#: An invitation is still "open" while the user has been asked and the
#: agent has not recorded an outcome.  A completed or declined check-in is
#: finished: the user's next message is ordinary conversation.
_OPEN_STATES = ("awaiting_reply", "replied", "in_progress")

#: Another recipient of the same step (the person or a contact) answered it.
ANSWERED_BY_OTHER = "answered_by_other"


async def open_invitation_for(
    session_factory: Any,
    *,
    org_id: uuid.UUID,
    agent_id: str,
    platform: str,
    platform_user_id: str,
) -> ProgramInvitationRow | None:
    """The newest open invitation this sender is answering, if any."""
    async with session_factory() as db:
        return (
            await db.execute(
                sa.select(ProgramInvitationRow)
                .where(ProgramInvitationRow.org_id == org_id)
                .where(ProgramInvitationRow.agent_id == agent_id)
                .where(ProgramInvitationRow.platform == platform)
                .where(
                    ProgramInvitationRow.platform_user_id == platform_user_id
                )
                .where(ProgramInvitationRow.response_state.in_(_OPEN_STATES))
                .order_by(ProgramInvitationRow.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


async def get_occurrence(
    session_factory: Any, occurrence_id: uuid.UUID,
) -> ProgramOccurrenceRow | None:
    async with session_factory() as db:
        return await db.get(ProgramOccurrenceRow, occurrence_id)


async def attach_reply(
    session_factory: Any,
    invitation: ProgramInvitationRow,
    *,
    session_id: uuid.UUID,
    bundle_version: str | None,
) -> None:
    """Bind this check-in to the session the reply is being handled in.

    Stamping ``session_id`` is what makes the outcome tools unspoofable: they
    find the invitation *by session*, so an agent in an ordinary conversation
    cannot close or escalate a check-in it is not part of.

    ``skill_bundle_version`` is recorded, not pinned — the skill itself is
    resolved live, so an operator correcting a wrong question reaches check-ins
    already under way.
    """
    async with session_factory() as db:
        row = await db.get(ProgramInvitationRow, invitation.id)
        if row is None:
            return
        row.response_state = "replied"
        row.session_id = session_id
        if bundle_version:
            row.skill_bundle_version = str(bundle_version)
        await db.commit()
    await _answered_for_all(session_factory, invitation)


async def _answered_for_all(session_factory: Any, invitation: Any) -> None:
    """An answer from the person or any contact answers the step for all of
    them: the others' copies stop waiting, and unsent ones are not sent.

    One row per recipient keeps each one's channel, deadline and reply
    matching their own; this is what makes them one step.
    """
    person = invitation.person_user_id or invitation.user_id
    others = (
        sa.update(ProgramInvitationRow)
        .where(ProgramInvitationRow.occurrence_id == invitation.occurrence_id)
        .where(ProgramInvitationRow.id != invitation.id)
        .where(sa.or_(
            ProgramInvitationRow.person_user_id == person,
            sa.and_(
                ProgramInvitationRow.person_user_id.is_(None),
                ProgramInvitationRow.user_id == person,
            ),
        ))
    )
    async with session_factory() as db:
        who = await db.get(User, invitation.user_id)
        link = (
            await db.execute(
                sa.select(FollowupContactRow.relation)
                .where(FollowupContactRow.org_id == invitation.org_id)
                .where(FollowupContactRow.agent_id == invitation.agent_id)
                .where(FollowupContactRow.person_user_id == person)
                .where(FollowupContactRow.contact_user_id == invitation.user_id)
            )
        ).scalars().first()
        name = (who.display_name if who else "") or "someone"
        reason = f"Answered by {name}" + (f" ({link})" if link else "")
        await db.execute(
            others.where(ProgramInvitationRow.response_state == "awaiting_reply")
            .values(response_state=ANSWERED_BY_OTHER, deadline_at=None, reason=reason)
        )
        await db.execute(
            others.where(ProgramInvitationRow.response_state == "not_started")
            .where(ProgramInvitationRow.delivery_state == "queued")
            .where(ProgramInvitationRow.outbox_id.is_(None))
            .values(
                response_state=ANSWERED_BY_OTHER, delivery_state="canceled", reason=reason,
            )
        )
        await db.commit()
