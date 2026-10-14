"""How far each step's invitations got: sent, delivered, read, answered."""

from __future__ import annotations

from datetime import datetime
from typing import Any, TypedDict

import sqlalchemy as sa

from surogates.db.models import ProgramInvitationRow

__all__ = ["Funnel", "step_funnel"]

_SKIPPED = ("skipped", "canceled")
_SENT = ("accepted", "delivered")
_REPLIED = ("replied", "in_progress", "completed", "incomplete", "declined")


class Funnel(TypedDict):
    invited: int
    skipped: int
    sent: int
    delivered: int
    read: int
    replied: int
    completed: int
    no_reply: int
    failed: int
    skip_reasons: dict[str, int]
    outcomes: dict[str, int]
    channels: dict[str, int]


def _empty() -> Funnel:
    return Funnel(
        invited=0, skipped=0, sent=0, delivered=0, read=0, replied=0, completed=0,
        no_reply=0, failed=0, skip_reasons={}, outcomes={}, channels={},
    )


async def step_funnel(
    session_factory: Any,
    *,
    org_id: Any,
    agent_id: str,
    program_ids: list,
    since: datetime,
) -> dict[Any, Funnel]:
    """Each Program's funnel over the invitations created since *since*.

    Keyed by the ids as given; a Program with no invitations still has a
    funnel of zeros.  A skipped person was never sent anything, so they count
    only towards ``skipped`` and its reasons.
    """
    funnels = {program_id: _empty() for program_id in program_ids}
    if not program_ids:
        return funnels
    by_id = {str(program_id): program_id for program_id in program_ids}
    read = ProgramInvitationRow.read_at.isnot(None)
    async with session_factory() as db:
        rows = (
            await db.execute(
                sa.select(
                    ProgramInvitationRow.program_id, ProgramInvitationRow.platform,
                    ProgramInvitationRow.delivery_state, ProgramInvitationRow.response_state,
                    read, ProgramInvitationRow.outcome, ProgramInvitationRow.reason,
                    sa.func.count(),
                )
                .where(ProgramInvitationRow.org_id == org_id)
                .where(ProgramInvitationRow.agent_id == agent_id)
                .where(ProgramInvitationRow.program_id.in_(list(program_ids)))
                .where(ProgramInvitationRow.created_at >= since)
                .group_by(
                    ProgramInvitationRow.program_id, ProgramInvitationRow.platform,
                    ProgramInvitationRow.delivery_state, ProgramInvitationRow.response_state,
                    # Grouped by the plain reason: a CASE with bound
                    # parameters in both lists is not the same expression
                    # to Postgres.  Only a skip's reason is read below.
                    read, ProgramInvitationRow.outcome, ProgramInvitationRow.reason,
                )
            )
        ).all()
    for program_id, platform, delivery, response, was_read, outcome, reason, n in rows:
        f = funnels[by_id[str(program_id)]]
        f["invited"] += n
        if delivery in _SKIPPED:
            f["skipped"] += n
            key = reason or "Skipped"
            f["skip_reasons"][key] = f["skip_reasons"].get(key, 0) + n
            continue
        f["channels"][platform] = f["channels"].get(platform, 0) + n
        if delivery in _SENT:
            f["sent"] += n
        if delivery == "delivered" or was_read:
            f["delivered"] += n
        if was_read:
            f["read"] += n
        if delivery in ("failed", "undelivered"):
            f["failed"] += n
        if response in _REPLIED:
            f["replied"] += n
        if response == "completed":
            f["completed"] += n
        if response == "no_reply_by_deadline":
            f["no_reply"] += n
        if outcome:
            f["outcomes"][outcome] = f["outcomes"].get(outcome, 0) + n
    return funnels
