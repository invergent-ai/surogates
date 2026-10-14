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
    funnel of zeros.  Counts people: a step that reached a person and their
    contacts is one, replied when any of them replied.  A skipped person was
    never sent anything, so they count only towards ``skipped`` and its
    reasons; ``channels`` counts the messages sent on each.
    """
    funnels = {program_id: _empty() for program_id in program_ids}
    if not program_ids:
        return funnels
    by_id = {str(program_id): program_id for program_id in program_ids}
    person = sa.func.coalesce(ProgramInvitationRow.person_user_id, ProgramInvitationRow.user_id)
    async with session_factory() as db:
        rows = (
            await db.execute(
                sa.select(
                    ProgramInvitationRow.program_id, ProgramInvitationRow.occurrence_id, person,
                    ProgramInvitationRow.platform, ProgramInvitationRow.delivery_state,
                    ProgramInvitationRow.response_state, ProgramInvitationRow.read_at.isnot(None),
                    ProgramInvitationRow.outcome, ProgramInvitationRow.reason,
                    ProgramInvitationRow.person_user_id.is_(None)
                    | (ProgramInvitationRow.person_user_id == ProgramInvitationRow.user_id),
                )
                .where(ProgramInvitationRow.org_id == org_id)
                .where(ProgramInvitationRow.agent_id == agent_id)
                .where(ProgramInvitationRow.program_id.in_(list(program_ids)))
                .where(ProgramInvitationRow.created_at >= since)
            )
        ).all()
    # One unit per person per occurrence: a step reaches the person and each
    # contact, and is answered when any of them answers.
    units: dict[tuple, list] = {}
    for program_id, occurrence_id, who, *rest in rows:
        units.setdefault((str(program_id), occurrence_id, who), []).append(rest)
    for (program_id, _occurrence, _who), copies in units.items():
        f = funnels[by_id[program_id]]
        f["invited"] += 1
        tried = [c for c in copies if c[1] not in _SKIPPED]
        if not tried:
            # The person's own row says why, when there is one.
            reason = next((c[5] for c in copies if c[6]), copies[0][5]) or "Skipped"
            f["skipped"] += 1
            f["skip_reasons"][reason] = f["skip_reasons"].get(reason, 0) + 1
            continue
        for platform, *_ in tried:
            f["channels"][platform] = f["channels"].get(platform, 0) + 1
        deliveries = {c[1] for c in tried}
        responses = {c[2] for c in tried}
        was_read = any(c[3] for c in tried)
        if deliveries & set(_SENT):
            f["sent"] += 1
        if "delivered" in deliveries or was_read:
            f["delivered"] += 1
        if was_read:
            f["read"] += 1
        if deliveries <= {"failed", "undelivered"}:
            f["failed"] += 1
        replied = bool(responses & set(_REPLIED))
        if replied:
            f["replied"] += 1
        if "completed" in responses:
            f["completed"] += 1
        if not replied and "no_reply_by_deadline" in responses:
            f["no_reply"] += 1
        outcome = next((c[4] for c in tried if c[4]), None)
        if outcome:
            f["outcomes"][outcome] = f["outcomes"].get(outcome, 0) + 1
    return funnels
