"""The tool an agent uses to close a journey step.

It takes no occurrence id.  The open step is found from the conversation:
the one bound to this session (the person answered here, or the agent was
woken here to write it), else the open step of the person this conversation
is with, on whichever of their channels it went out.  An agent in an
ordinary conversation cannot close a step of somebody else's.

``step_outcome`` is the advertised name; ``checkin_outcome`` is its older
name, mapped onto it by :data:`surogates.runtime.governance.RENAMED_TOOLS`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa

from surogates.db.models import EscalationRow, ProgramInvitationRow, Session as SessionRow
from surogates.followup.missed import HANDLED_BY_AGENT
from surogates.programs.inbound import MOST_RECENTLY_OPENED
from surogates.tools.registry import ToolSchema

#: Response states of a step the person has answered and the agent has not closed.
_OPEN = ("replied", "in_progress")

#: How long after the agent was woken at a step's deadline it may still
#: record what it decided, or a late answer.
_DECIDE_WITHIN = timedelta(hours=24)

_VALID_OUTCOMES = ("completed", "declined", "skipped")


STEP_OUTCOME_SCHEMA = ToolSchema(
    name="step_outcome",
    description=(
        "Record how a journey step ended. 'outcome' is the status: "
        "'completed' when you got the answers, even partly; 'declined' when "
        "the person refused or asked to be left alone; 'skipped' when you "
        "decided the step needs nothing from the person (before writing it, or "
        "after its deadline passed with no reply), with the reason in 'note'. "
        "'label' is a short category for what the answers showed, in the "
        "business's own words as the step's instructions define them (e.g. "
        "green, yellow or red; hot, warm or cold). Pass a label whenever the "
        "instructions name such categories. This closes the step; it does not "
        "message anyone and does not alert the team (use escalate for that)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "outcome": {
                "type": "string",
                "enum": list(_VALID_OUTCOMES),
                "description": "The status: completed, declined, or skipped by you.",
            },
            "label": {
                "type": "string",
                "description": (
                    "One or two words from the categories the step's "
                    "instructions define, e.g. green. Not the status, and none "
                    "with 'skipped'. When "
                    "omitted and you escalated during this step, the "
                    "escalation's severity is recorded instead."
                ),
            },
            "note": {
                "type": "string",
                "description": (
                    "Short note for the team's history. Required with "
                    "'skipped': why the step is not needed."
                ),
            },
        },
        "required": ["outcome"],
    },
)


async def _open_invitation_for_session(session_factory: Any, session_id: Any):
    """The step this conversation can close or escalate, if any.

    In this session or with this conversation's person (as the recipient or
    as the followed person), on any channel:

    * answered and not closed;
    * woken here for the agent to write, not yet answered (it may skip it);
    * woken here or for this person at its deadline, recently (the agent
      records its decision, or a late answer).

    Several open: the most recently opened, as a reply is matched.
    """
    async with session_factory() as db:
        session = await db.get(SessionRow, uuid.UUID(str(session_id)))
        if session is None:
            return None
        mine = [ProgramInvitationRow.session_id == session.id]
        if session.user_id is not None:
            mine.append(ProgramInvitationRow.user_id == session.user_id)
            mine.append(ProgramInvitationRow.person_user_id == session.user_id)
        since = datetime.now(timezone.utc) - _DECIDE_WITHIN
        return (
            await db.execute(
                sa.select(ProgramInvitationRow)
                .where(ProgramInvitationRow.org_id == session.org_id)
                .where(ProgramInvitationRow.agent_id == session.agent_id)
                .where(sa.or_(*mine))
                .where(sa.or_(
                    ProgramInvitationRow.response_state.in_(_OPEN),
                    sa.and_(
                        ProgramInvitationRow.response_state == "awaiting_reply",
                        ProgramInvitationRow.woken_at.isnot(None),
                        ProgramInvitationRow.session_id == session.id,
                    ),
                    sa.and_(
                        ProgramInvitationRow.response_state == "no_reply_by_deadline",
                        ProgramInvitationRow.deadline_woken_at >= since,
                    ),
                ))
                .order_by(*MOST_RECENTLY_OPENED)
                .limit(1)
            )
        ).scalars().first()


async def handle_checkin_outcome(arguments: dict, **kwargs: Any) -> str:
    outcome = str(arguments.get("outcome") or "").strip().lower()
    if outcome not in _VALID_OUTCOMES:
        return "outcome must be 'completed', 'declined' or 'skipped'."
    note = str(arguments.get("note") or "").strip()[:500]
    if outcome == "skipped" and not note:
        return "Say why in note: a skipped step needs the reason for the team."

    sf = kwargs["session_factory"]
    inv = await _open_invitation_for_session(sf, kwargs["session_id"])
    if inv is None:
        return "No scheduled step is open in this conversation."
    if outcome == "skipped" and inv.response_state in _OPEN:
        return (
            "The person has answered this step: record 'completed' or "
            "'declined' instead."
        )

    # ``result`` is the label's older name.
    label = str(arguments.get("label") or arguments.get("result") or "").strip().lower()[:100]
    if outcome == "skipped":
        label = ""  # a step not done has no result to categorise
    async with sf() as db:
        if not label and outcome != "skipped":
            severities = set((await db.execute(
                sa.select(EscalationRow.severity)
                .where(EscalationRow.invitation_id == inv.id)
                .where(EscalationRow.severity.isnot(None))
            )).scalars())
            label = "red" if "red" in severities else "yellow" if severities else ""
        row = await db.get(ProgramInvitationRow, inv.id)
        if outcome != "skipped":
            row.response_state = outcome
        elif row.response_state == "no_reply_by_deadline":
            # The silence stays on record; what the agent did about it is
            # its decision, and the team's automatic alert stands down.
            if row.escalation_state == "none":
                row.escalation_state = HANDLED_BY_AGENT
        else:
            row.response_state = "skipped_by_agent"
            row.deadline_at = None
            if row.outbox_id is None:
                row.delivery_state = "skipped"
        row.outcome = None if outcome == "skipped" else label or row.outcome
        if note:
            row.reason = note
        await db.commit()
    return (
        f"Step recorded as {outcome}" + (f", label {label}" if label else "")
        + ". This note is for you, not the person: do not mention it to them."
    )


def register(registry: Any) -> None:
    """Register the step_outcome tool.

    Escalation is ``escalate`` in :mod:`surogates.tools.builtin.followup`: it
    works inside a step and outside one.
    """
    registry.register(
        name="step_outcome",
        schema=STEP_OUTCOME_SCHEMA,
        handler=handle_checkin_outcome,
        toolset="checkin",
    )
