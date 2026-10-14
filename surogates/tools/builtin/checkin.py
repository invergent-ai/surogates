"""The tool an agent uses to close a check-in.

It takes no occurrence id.  The open invitation is found **by session**,
because the inbound path stamped ``session_id`` on it when the user replied.
That is what makes it unspoofable: an agent in an ordinary conversation
cannot close a check-in it is not inside.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from surogates.db.models import EscalationRow, ProgramInvitationRow
from surogates.tools.registry import ToolSchema

#: Response states a check-in can still be closed or escalated from.
_OPEN = ("replied", "in_progress")

_VALID_OUTCOMES = ("completed", "declined")

CHECKIN_OUTCOME_SCHEMA = ToolSchema(
    name="checkin_outcome",
    description=(
        "Record how a scheduled step ended, once the person has answered its "
        "questions or has clearly declined to. Two separate things: "
        "'outcome' is the status ('completed' when you got the answers, even "
        "partly; 'declined' when the person refused or asked to be left "
        "alone); 'label' is a short category for what the answers showed, in "
        "the business's own words as the step's instructions define them "
        "(e.g. green, yellow or red; hot, warm or cold). Pass a label whenever "
        "the instructions name such categories. This closes the step; it "
        "does not message anyone and does not alert the team (use escalate "
        "for that)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "outcome": {
                "type": "string",
                "enum": list(_VALID_OUTCOMES),
                "description": "The status: whether the step was completed or declined.",
            },
            "label": {
                "type": "string",
                "description": (
                    "One or two words from the categories the step's "
                    "instructions define, e.g. green. Not the status. When "
                    "omitted and you escalated during this step, the "
                    "escalation's severity is recorded instead."
                ),
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
        return "No scheduled step is open in this conversation."

    # ``result`` is the label's older name.
    label = str(arguments.get("label") or arguments.get("result") or "").strip().lower()[:100]
    async with sf() as db:
        if not label:
            severities = set((await db.execute(
                sa.select(EscalationRow.severity)
                .where(EscalationRow.invitation_id == inv.id)
                .where(EscalationRow.severity.isnot(None))
            )).scalars())
            label = "red" if "red" in severities else "yellow" if severities else ""
        row = await db.get(ProgramInvitationRow, inv.id)
        row.response_state = outcome
        row.outcome = label or None
        if arguments.get("note"):
            row.reason = str(arguments["note"])[:500]
        await db.commit()
    return (
        f"Step recorded as {outcome}" + (f", label {label}" if label else "")
        + ". This note is for you, not the person: do not mention it to them."
    )


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
