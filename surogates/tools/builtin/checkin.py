"""Tools an agent uses to close or escalate a check-in.

Neither tool takes an occurrence id.  The open invitation is found **by
session**, because the inbound path stamped ``session_id`` on it when the
user replied.  That is what makes these unspoofable: an agent in an
ordinary conversation cannot close or escalate a check-in it is not inside.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from surogates.db.models import (
    Event,
    InboxItem,
    ProgramInvitationRow,
    ProgramScheduleRow,
)
from surogates.tools.registry import ToolSchema

logger = logging.getLogger(__name__)

#: Response states a check-in can still be closed from.
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

CHECKIN_ESCALATE_SCHEMA = ToolSchema(
    name="checkin_escalate",
    description=(
        "Raise a scheduled check-in to the responsible operator. Use this when "
        "something in the person's answers needs a human's attention — a "
        "worrying symptom, a reading outside safe bounds, distress. This does "
        "not end the check-in; record the outcome separately when it ends."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": (
                    "What needs attention, in one or two sentences the "
                    "operator can act on."
                ),
            },
        },
        "required": ["reason"],
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


async def handle_checkin_escalate(arguments: dict, **kwargs: Any) -> str:
    reason = str(arguments.get("reason") or "").strip()
    if not reason:
        return "reason is required — say what needs the operator's attention."

    sf = kwargs["session_factory"]
    inv = await _open_invitation_for_session(sf, kwargs["session_id"])
    if inv is None:
        return "No check-in is open in this conversation."

    async with sf() as db:
        sched = (
            await db.execute(
                sa.select(ProgramScheduleRow).where(
                    ProgramScheduleRow.program_id == inv.program_id
                )
            )
        ).scalar_one_or_none()
        target = ((sched.config if sched else None) or {}).get(
            "escalation_service_account_id"
        )
        row = await db.get(ProgramInvitationRow, inv.id)

        try:
            target_id = uuid.UUID(str(target)) if target else None
        except ValueError:
            target_id = None
        if target_id is None:
            # A visible failure, never a silent one: the agent saying it
            # escalated is not evidence that anyone was told.
            row.escalation_state = "failed"
            row.reason = "No escalation target configured on this Program"
            await db.commit()
            return (
                "Could not escalate: this Program has no responsible operator."
            )

        # inbox_items.source_event_id is NOT NULL and unique, so the
        # escalation is first an event on the user's own session.
        event = Event(
            session_id=inv.session_id,
            org_id=inv.org_id,
            type="checkin.escalation",
            data={"reason": reason, "occurrence_id": str(inv.occurrence_id)},
        )
        db.add(event)
        await db.flush()
        db.add(
            InboxItem(
                org_id=inv.org_id,
                # The designated operator, never the acting principal: an item
                # raised against the user reaches nobody but the user.
                user_id=None,
                service_account_id=target_id,
                session_id=inv.session_id,
                source_event_id=event.id,
                # An existing InboxKind. A new value would never pass the ops
                # Inbox filter, which is a Literal over the known kinds.
                kind="action_required",
                title="Check-in needs your attention",
                body=reason,
                payload={
                    "program_id": str(inv.program_id),
                    "occurrence_id": str(inv.occurrence_id),
                    "user_id": str(inv.user_id),
                },
            )
        )
        # Escalation is its own axis: raising a hand does not mean the user
        # has finished answering, so response_state is deliberately untouched.
        row.escalation_state = "raised"
        try:
            await db.commit()
        except IntegrityError:
            # The configured target is not a service account this database
            # knows — a human's id pasted where a principal belongs, say. The
            # insert rolled back, taking the event and the "raised" mark with
            # it. Record the failure in its own transaction so the raised
            # hand is not simply lost.
            await db.rollback()
            logger.warning(
                "[programs] escalation target %s for program %s is not a "
                "service account", target_id, inv.program_id,
            )
            async with sf() as db2:
                row2 = await db2.get(ProgramInvitationRow, inv.id)
                row2.escalation_state = "failed"
                row2.reason = "Escalation target is not a valid service account"
                await db2.commit()
            return (
                "Could not escalate: this Program's responsible operator is "
                "not configured correctly."
            )

    return "Escalated to the responsible operator."


def register(registry: Any) -> None:
    """Register the checkin_outcome and checkin_escalate tools."""
    registry.register(
        name="checkin_outcome",
        schema=CHECKIN_OUTCOME_SCHEMA,
        handler=handle_checkin_outcome,
        toolset="checkin",
    )
    registry.register(
        name="checkin_escalate",
        schema=CHECKIN_ESCALATE_SCHEMA,
        handler=handle_checkin_escalate,
        toolset="checkin",
    )
