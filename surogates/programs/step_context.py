"""The block the agent reads on every turn a journey step causes.

A step wakes the agent three ways: it is due and the agent writes it, the
person answers it, or its deadline passes with no answer.  Each turn opens
with this block, so the agent knows which journey and step it is in, what day
it is for the person and where, who is answering, how the last steps went and
why it is awake.  It is written into the conversation as a synthetic user
message under :data:`STEP_MARKER`, so the model never reads it as the
person's words.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from surogates.db.models import Event, ProgramInvitationRow, ProgramScheduleRow, User
from surogates.followup import store as followup_store
from surogates.followup.eligibility import as_utc, day_index
from surogates.programs.journey import journey_of, person_zone, protocol_of
from surogates.session.events import SILENT_REPLY

logger = logging.getLogger(__name__)

__all__ = ["STEP_MARKER", "step_context"]

#: ``synthetic`` value of the message carrying the block.
STEP_MARKER = "checkin"

#: Why the agent is awake.
DUE, REPLY, DEADLINE = "due", "reply", "deadline"

_CLOSED = (
    "completed", "declined", "skipped_by_agent", "no_reply_by_deadline", "incomplete",
    "answered_by_other",
)
_SAID = {
    "completed": "completed", "declined": "declined", "skipped_by_agent": "skipped by you",
    "no_reply_by_deadline": "no reply", "incomplete": "answered, not finished",
    "answered_by_other": "answered by someone else",
}


async def step_context(
    session_factory: Any,
    invitation: Any,
    *,
    event: str,
    now: datetime,
    config: dict | None = None,
    session_id: Any = None,
    can_write: bool = True,
    writer_user_id: Any = None,
) -> str:
    """The step context block for *invitation*, woken for *event*.

    *can_write* is False when the channel would refuse a message from the
    agent right now (its service window is closed).  *writer_user_id* is who
    is writing when not the step's recipient (a reply from another of the
    person's channels, or from a contact).
    """
    if config is None:
        async with session_factory() as db:
            config = (
                await db.execute(
                    sa.select(ProgramScheduleRow.config)
                    .where(ProgramScheduleRow.program_id == invitation.program_id)
                )
            ).scalar_one_or_none() or {}
    person_id = invitation.person_user_id or invitation.user_id
    protocol = protocol_of(config)
    found = await followup_store.enrollments(
        session_factory, org_id=invitation.org_id, agent_id=invitation.agent_id,
        user_id=person_id, protocol=protocol,
    ) if protocol else []
    enrollment = found[0] if found else None
    zone = person_zone(enrollment, config)
    local = as_utc(now).astimezone(ZoneInfo(zone))
    journey = journey_of(config)
    skill = str(config.get("skill_ref") or "").strip()

    lines = ["[Step context, from the platform; the person did not write this]"]
    lines.append(
        f"Journey: {journey.get('name') or protocol or 'none'}"
        f" · Step: {config.get('name') or skill or 'unnamed'}"
    )
    index = day_index(getattr(enrollment, "start_date", None), local.date())
    when = f"Today for them: {local:%A %Y-%m-%d %H:%M} ({zone})"
    if index is not None:
        start = enrollment.start_date.isoformat()
        when = (
            f"Day {index} of the journey (day 0 is {start}"
            + (", so this is before it" if index < 0 else "")
            + f") · {when}"
        )
    lines.append(when)
    lines.append(await _who(
        session_factory, invitation, person_id, enrollment, writer_user_id or invitation.user_id,
    ))
    lines.append(f"Recent steps: {await _recent(session_factory, invitation)}")
    answers = await _last_form_answers(session_factory, session_id or invitation.session_id)
    if answers:
        lines.append(f"Last form answers: {answers}")
    lines.append(_why(event, config, skill, can_write))
    return "\n".join(lines)


async def _who(
    session_factory: Any, invitation: Any, person_id: Any, enrollment: Any, writer_id: Any,
) -> str:
    """The person followed, and who is writing: them or one of their contacts."""
    async with session_factory() as db:
        person = await db.get(User, person_id)
    name = (
        getattr(enrollment, "patient_name", None) or getattr(person, "display_name", None) or ""
    ).strip()
    text = f"Person: {name or 'name not recorded'}"
    speaker = await followup_store.speaker_for(
        session_factory, org_id=invitation.org_id, agent_id=invitation.agent_id,
        user_id=writer_id, person_user_id=person_id,
    )
    if speaker is not None:
        text += f" · {followup_store.writer_line(speaker)}"
    if getattr(enrollment, "answered_by", None) and getattr(speaker, "role", "person") == "person":
        text += f" · Usually answered by {enrollment.answered_by}"
    return text


async def _recent(session_factory: Any, invitation: Any) -> str:
    """The last three closed steps of this Program with this person."""
    async with session_factory() as db:
        rows = (
            await db.execute(
                sa.select(
                    ProgramInvitationRow.created_at,
                    ProgramInvitationRow.response_state,
                    ProgramInvitationRow.outcome,
                )
                .where(ProgramInvitationRow.program_id == invitation.program_id)
                .where(ProgramInvitationRow.user_id == invitation.user_id)
                .where(ProgramInvitationRow.id != invitation.id)
                .where(ProgramInvitationRow.response_state.in_(_CLOSED))
                .order_by(ProgramInvitationRow.created_at.desc())
                .limit(3)
            )
        ).all()
    if not rows:
        return "none yet"
    return " · ".join(
        f"{as_utc(at):%Y-%m-%d} {_SAID.get(state, state)}" + (f" ({label})" if label else "")
        for at, state, label in rows
    )


async def _last_form_answers(session_factory: Any, session_id: Any) -> str | None:
    """The newest form the person submitted in this conversation."""
    if session_id is None:
        return None
    async with session_factory() as db:
        rows = (
            await db.execute(
                sa.select(Event.data)
                .where(Event.session_id == session_id)
                .where(Event.type == "user.message")
                .order_by(Event.id.desc())
                .limit(200)
            )
        ).scalars().all()
    for data in rows:
        source = (data or {}).get("source")
        # A message's source is a dict for a form's answer, a string for others (a mention, an inbox decision).
        if isinstance(source, dict) and source.get("flow_reply"):
            return " ".join(str(data.get("content") or "").split())[:600] or None
    return None


def _why(event: str, config: dict, skill: str, can_write: bool) -> str:
    use_skill = f"Use the '{skill}' skill. " if skill else ""
    if event == DUE:
        wording = str(config.get("opener_text") or "").strip()
        return (
            "Why you are awake: this step is due now. Write its message to the "
            f"person yourself, in this conversation. {use_skill}"
            + (f"Default wording you may use or adapt: «{wording}». " if wording else "")
            + "If the person needs no message for this step, call step_outcome "
            "with outcome 'skipped' and the reason in note, then reply with exactly "
            f"{SILENT_REPLY} so nothing is sent."
        )
    if event == DEADLINE:
        hours = config.get("response_deadline_hours")
        return (
            "Why you are awake: the step's deadline"
            + (f" ({hours}h)" if hours else "")
            + " passed with no reply. Decide: write a short nudge, alert the team "
            "with escalate, pause or move their journey with followup_update, or "
            "do nothing. Nothing reaches the person unless you write it; to write "
            f"nothing reply with exactly {SILENT_REPLY}. If you escalate, or record "
            "a decision with step_outcome (outcome 'skipped' with the reason), "
            "the platform's automatic missed-reply alert is not sent."
            + (
                "" if can_write else
                " The channel's service window is closed: a message from you would "
                "not reach them now."
            )
        )
    return (
        "Why you are awake: the person is answering this step. "
        + (f"Run the '{skill}' skill with them now. " if skill else "")
        + "Record the result with step_outcome when the step ends; "
        "use escalate if anything needs the team's attention."
    )
