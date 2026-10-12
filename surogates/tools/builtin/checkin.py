"""The tool an agent uses to close a check-in.

It takes no occurrence id.  The open invitation is found **by session**,
because the inbound path stamped ``session_id`` on it when the user replied.
That is what makes it unspoofable: an agent in an ordinary conversation
cannot close a check-in it is not inside.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from surogates.db.models import ProgramInvitationRow
from surogates.tools.registry import ToolSchema

#: Response states a check-in can still be closed or escalated from.
_OPEN = ("replied", "in_progress")

_VALID_OUTCOMES = ("completed", "declined")

CHECKIN_OUTCOME_SCHEMA = ToolSchema(
    name="checkin_outcome",
    description=(
        "Record how a scheduled check-in ended. Call this once the person has "
        "answered the check-in questions, or has clearly declined to. Use "
        "'completed' when you got the answers, 'declined' when the person "
        "refused or asked to be left alone. This closes the check-in; it does "
        "not message anyone."
    ),
    parameters={
        "type": "object",
        "properties": {
            "outcome": {
                "type": "string",
                "enum": list(_VALID_OUTCOMES),
                "description": "Whether the check-in was completed or declined.",
            },
            "note": {
                "type": "string",
                "description": (
                    "Optional short note for the operator's run history."
                ),
            },
        },
        "required": ["outcome"],
    },
)

async def _open_invitation_for_session(session_factory: Any, session_id: Any):
    async with session_factory() as db:
        return (
            await db.execute(
                sa.select(ProgramInvitationRow)
                .where(ProgramInvitationRow.session_id == session_id)
                .where(ProgramInvitationRow.response_state.in_(_OPEN))
                .order_by(ProgramInvitationRow.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


async def handle_checkin_outcome(arguments: dict, **kwargs: Any) -> str:
    outcome = str(arguments.get("outcome") or "").strip().lower()
    if outcome not in _VALID_OUTCOMES:
        return "outcome must be 'completed' or 'declined'."

    sf = kwargs["session_factory"]
    inv = await _open_invitation_for_session(sf, kwargs["session_id"])
    if inv is None:
        return "No check-in is open in this conversation."

    async with sf() as db:
        row = await db.get(ProgramInvitationRow, inv.id)
        row.response_state = outcome
        if arguments.get("note"):
            row.reason = str(arguments["note"])[:500]
        await db.commit()
    return f"Check-in recorded as {outcome}."


def register(registry: Any) -> None:
    """Register the checkin_outcome tool.

    Escalation is ``escalate`` in :mod:`surogates.tools.builtin.followup`: it
    works inside a check-in and outside one.
    """
    registry.register(
        name="checkin_outcome",
        schema=CHECKIN_OUTCOME_SCHEMA,
        handler=handle_checkin_outcome,
        toolset="checkin",
    )
