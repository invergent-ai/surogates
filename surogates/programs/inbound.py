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

from surogates.db.models import ProgramInvitationRow, ProgramOccurrenceRow

#: An invitation is still "open" while the patient has been asked and the
#: agent has not recorded an outcome.  A completed or declined check-in is
#: finished: the patient's next message is ordinary conversation.
_OPEN_STATES = ("awaiting_reply", "replied", "in_progress")


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
    resolved live, so a doctor correcting a wrong question reaches check-ins
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
