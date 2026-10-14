"""Tell the team when someone has stopped answering.

A Program can set ``alert_after_missed: N``.  When a person's last ``N``
openers from that Program all went unanswered, every alert recipient of the
agent is told, on their own channel.  Silence is the one signal the person cannot send
themselves: someone who fainted does not reply "I fainted".

Runs right after the deadline sweep, which is what turns an unanswered opener
into ``no_reply_by_deadline``.  Each alert is keyed on the invitation that
completed the streak, so a re-scan never alerts twice.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa

from surogates.db.models import (
    DeliveryOutbox,
    Event,
    ProgramInvitationRow,
    ProgramScheduleRow,
)
from surogates.channels.capabilities import capabilities_for
from surogates.channels.identity import readable_phone
from surogates.followup import store as followup_store
from surogates.followup.eligibility import as_utc
from surogates.followup.outreach import describe_people
from surogates.programs.journey import own_channel

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MISSED_TEXT",
    "alert_missed_checkins",
    "paused_by_silence",
    "render_missed_text",
    "threshold",
]

#: How far back a newly unanswered opener is looked at: long enough that a
#: ticker down for a few hours still alerts, short enough that a pass does
#: not re-read the same silence all day.
LOOKBACK = timedelta(hours=6)

#: The agent is woken at each deadline and may handle the silence itself;
#: the team's alert waits this long after that wake (however late quiet
#: hours made it) and stands down if the agent acted (see :func:`_agent_acted`).
AGENT_GRACE = timedelta(minutes=10)

#: How long after its deadline a step's agent may still be woken.  A step
#: never woken by then (no send pass ran) falls back to the platform's alert.
DEADLINE_WAKE_WINDOW = timedelta(hours=12)

#: Set on a step's escalation axis when the agent dealt with the silence
#: itself; the platform's missed-reply alert then stands down.
HANDLED_BY_AGENT = "handled_by_agent"

#: Openers whose streak this process already judged, with their deadline.
_JUDGED: dict[Any, datetime] = {}

#: Used when the Program carries no wording of its own.  Placeholders:
#: ``{name}``, ``{phone}``, ``{count}``, ``{program}``.
DEFAULT_MISSED_TEXT = (
    "{name} ({phone}) has not answered the last {count} messages of "
    "“{program}”."
)


def render_missed_text(template: str | None, **values: Any) -> str:
    """The alert text, never failing on a malformed operator template."""
    return followup_store.render_text(template, DEFAULT_MISSED_TEXT, **values)


def threshold(config: dict, key: str = "alert_after_missed") -> int | None:
    """A Program's positive whole-number setting, or ``None`` when unset."""
    raw = (config or {}).get(key)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return None
    return raw


async def alert_missed_checkins(session_factory: Any, *, now: datetime) -> int:
    """Queue alerts for every streak of unanswered openers.  Returns how many."""
    async with session_factory() as db:
        # Only Programs that ask for it: everything else is never read.  The
        # SQL keeps the configs that set the key at all (``->>`` is NULL for
        # a missing key and a JSON null alike); ``threshold`` still judges
        # the value.
        watched = {
            pid: cfg for pid, cfg in (
                await db.execute(
                    sa.select(ProgramScheduleRow.program_id, ProgramScheduleRow.config)
                    .where(ProgramScheduleRow.active.is_(True))
                    .where(
                        ProgramScheduleRow.config["alert_after_missed"]
                        .as_string().isnot(None)
                    )
                )
            ).all()
            if threshold(cfg) is not None
        }
        if not watched:
            return 0
        candidates = (
            await db.execute(
                sa.select(ProgramInvitationRow)
                .where(ProgramInvitationRow.program_id.in_(list(watched)))
                .where(ProgramInvitationRow.response_state == "no_reply_by_deadline")
                .where(ProgramInvitationRow.escalation_state == "none")
                .where(ProgramInvitationRow.deadline_at.isnot(None))
                # Never before the agent's own deadline wake has had its
                # turn, even when quiet hours put that wake off until morning.
                .where(sa.or_(
                    sa.and_(
                        ProgramInvitationRow.deadline_woken_at.isnot(None),
                        ProgramInvitationRow.deadline_woken_at <= now - AGENT_GRACE,
                        ProgramInvitationRow.deadline_woken_at >= now - LOOKBACK,
                    ),
                    sa.and_(
                        ProgramInvitationRow.deadline_woken_at.is_(None),
                        ProgramInvitationRow.deadline_at <= now - DEADLINE_WAKE_WINDOW,
                        ProgramInvitationRow.deadline_at >= now - DEADLINE_WAKE_WINDOW - LOOKBACK,
                    ),
                ))
                .order_by(ProgramInvitationRow.created_at)
            )
        ).scalars().all()

    # Only the newest unanswered opener of each person in each Program can
    # complete a streak; the older ones were judged when they were newest.
    newest: dict[tuple, Any] = {}
    for inv in candidates:
        newest[(inv.program_id, _person(inv))] = inv
    # A streak is judged once.  The lookback keeps an opener a candidate for
    # hours, and without this every pass (each minute) would re-run the
    # streak query for every one of them.  Kept per process and pruned with
    # the lookback; a restart re-judges once, which is harmless because
    # alerts are deduplicated.
    for inv_id, deadline in list(_JUDGED.items()):
        if deadline < now - LOOKBACK - DEADLINE_WAKE_WINDOW:
            del _JUDGED[inv_id]

    settings_cache: dict[tuple, Any] = {}
    queued = 0
    for inv in newest.values():
        if inv.id in _JUDGED:
            continue
        config = watched[inv.program_id]
        try:
            if await _agent_acted(session_factory, inv, config):
                _JUDGED[inv.id] = as_utc(inv.deadline_at)
                continue
            queued += await _alert_if_streak(
                session_factory, inv, config, threshold(config), settings_cache,
            )
        except Exception:  # noqa: BLE001 — one person, not the pass
            logger.exception(
                "[followup] could not check the missed streak for invitation %s",
                inv.id,
            )
            continue
        _JUDGED[inv.id] = as_utc(inv.deadline_at)
    return queued


async def _agent_acted(session_factory: Any, inv: Any, config: dict) -> bool:
    """Whether the agent, woken at this step's deadline, dealt with it.

    Escalating or recording the step already moved it off ``none``; here the
    other two: a message it sent the person after that wake (a nudge), or the
    person's journey paused or ended.  Either is marked on the step, so the
    streak it would have completed is not reported.
    """
    if inv.deadline_woken_at is None:
        return False
    acted = False
    protocol = _protocol(config)
    if protocol:
        found = await followup_store.enrollments(
            session_factory, org_id=inv.org_id, agent_id=inv.agent_id,
            user_id=_person(inv), protocol=protocol,
        )
        acted = bool(found) and found[0].status != "active"
    if not acted and inv.session_id is not None:
        acted = await _nudged(session_factory, inv)
    if acted:
        async with session_factory() as db:
            await db.execute(
                sa.update(ProgramInvitationRow)
                .where(ProgramInvitationRow.id == inv.id)
                .where(ProgramInvitationRow.escalation_state == "none")
                .values(escalation_state=HANDLED_BY_AGENT)
            )
            await db.commit()
    return acted


def _protocol(config: dict) -> str | None:
    audience = (config or {}).get("audience") or {}
    journey = (config or {}).get("journey") or {}
    return (audience.get("protocol") if isinstance(audience, dict) else None) or journey.get("slug")


async def _nudged(session_factory: Any, inv: Any) -> bool:
    """A message reached the outbox in the step's conversation after the
    agent's deadline wake: the wake's own event orders it, not a clock."""
    async with session_factory() as db:
        events = (
            await db.execute(
                sa.select(Event.id, Event.data)
                .where(Event.session_id == inv.session_id)
                .where(Event.type == "user.message")
                .order_by(Event.id.desc())
                .limit(50)
            )
        ).all()
        wake = next(
            (
                eid for eid, data in events
                if (data or {}).get("invitation_id") == str(inv.id)
                and (data or {}).get("step_event") == "deadline"
            ),
            None,
        )
        if wake is None:
            return False
        return (
            await db.execute(
                sa.select(DeliveryOutbox.id)
                .where(DeliveryOutbox.session_id == inv.session_id)
                .where(DeliveryOutbox.event_id > wake)
                .limit(1)
            )
        ).first() is not None


def _person(inv: Any) -> Any:
    """The followed person an invitation is about: its recipient, or the
    person whose contact the recipient is."""
    return inv.person_user_id or inv.user_id


def _about(person_id: Any):
    """Rows about *person_id*: theirs, or a contact's copy of their step."""
    return sa.or_(
        ProgramInvitationRow.person_user_id == person_id,
        sa.and_(
            ProgramInvitationRow.person_user_id.is_(None),
            ProgramInvitationRow.user_id == person_id,
        ),
    )


async def _alert_if_streak(
    session_factory: Any, inv: Any, config: dict, threshold: int, settings_cache: dict,
) -> int:
    person_id = _person(inv)
    async with session_factory() as db:
        rows = (
            await db.execute(
                sa.select(
                    ProgramInvitationRow.occurrence_id,
                    ProgramInvitationRow.response_state,
                    ProgramInvitationRow.escalation_state,
                )
                .where(ProgramInvitationRow.program_id == inv.program_id)
                .where(_about(person_id))
                .where(ProgramInvitationRow.created_at <= inv.created_at)
                # Skips are not silence: the person was never asked.
                .where(ProgramInvitationRow.delivery_state.notin_(("skipped", "canceled")))
                .order_by(ProgramInvitationRow.created_at.desc())
                .limit(threshold * 8)
            )
        ).all()
    # One step per occurrence, whoever of the person and their contacts it
    # reached: unanswered only when none of them answered.
    steps: dict[Any, list] = {}
    for occurrence_id, response, escalation in rows:
        steps.setdefault(occurrence_id, []).append((response, escalation))
    recent = list(steps.values())[:threshold]
    if len(recent) < threshold or any(
        r != "no_reply_by_deadline" for step in recent for r, _ in step
    ):
        return 0
    # A streak already reported does not count again: the next alert needs
    # another full run of unanswered openers, not one more on top of it.
    if any(e != "none" for step in recent[1:] for _, e in step):
        return 0

    key = (inv.org_id, inv.agent_id)
    if key not in settings_cache:
        settings_cache[key] = await followup_store.get_settings(
            session_factory, org_id=inv.org_id, agent_id=inv.agent_id,
        )
    [person] = (await describe_people(
        session_factory, org_id=inv.org_id, agent_id=inv.agent_id, user_ids=[person_id],
    )).values()
    # A Slack member id or a Telegram chat id is no phone, nor is a
    # contact's number the person's: then the person's WhatsApp number.
    phone = (
        inv.platform_user_id
        if capabilities_for(inv.platform).identity == "phone" and person_id == inv.user_id
        else person.wa_id
    )
    text = render_missed_text(
        config.get("missed_alert_text"),
        name=person.label(settings_cache[key]) or "?",
        phone=readable_phone(phone) if phone else "?",
        count=threshold,
        program=str(config.get("name") or "step"),
    )
    queued, _why_not = await followup_store.notify_team(
        session_factory, settings_cache[key], org_id=inv.org_id,
        agent_id=inv.agent_id, kind="alert", text=text,
        created_by="missed-checkins", subject_user_id=person_id,
        dedupe_prefix=f"missed:{inv.id}", topic="missed_replies",
        # The Program's own sender, on its own channel: a Slack Program's
        # team id is no WhatsApp sender.
        fallback_senders={
            own_channel(config): str(config.get("channel_identifier") or ""),
        },
    )

    # Keyed on the invitation, so a pass retried after a crash updates the
    # one record instead of queuing the team a second item.
    await followup_store.record_escalation(
        session_factory, escalation_id=inv.id, org_id=inv.org_id,
        agent_id=inv.agent_id, user_id=person_id, kind="missed_replies",
        reason=text, state="raised" if queued else "failed", platform=inv.platform,
        invitation_id=inv.id, program_id=inv.program_id, session_id=inv.session_id,
    )
    # Visible, never silent: when nobody could be told, the history says so
    # and the pass moves on.  Every copy of the step is marked, so a
    # contact's copy is not judged again as a streak of its own.
    async with session_factory() as db:
        await db.execute(
            sa.update(ProgramInvitationRow)
            .where(ProgramInvitationRow.occurrence_id == inv.occurrence_id)
            .where(_about(person_id))
            .where(ProgramInvitationRow.escalation_state == "none")
            .values(
                escalation_state="raised" if queued else "failed",
                reason=(
                    f"No answer to {threshold} openers in a row; team alerted" if queued
                    else f"No answer to {threshold} openers in a row; nobody on the "
                    "team could be told"
                ),
            )
        )
        await db.commit()
    return queued


async def paused_by_silence(
    session_factory: Any,
    *,
    program_id: Any,
    user_id: Any,
    count: int,
    last_inbound_at: datetime | None,
) -> bool:
    """Whether this Program should stop writing to someone for now.

    True when the *count* openers it sent since the person last wrote all
    went unanswered.  Writing starts the count over: after a message, the
    pause needs *count* new misses, not one more on top of the old streak.
    Skipped openers do not count either way — the person was never asked —
    so a pause holds until they write instead of lapsing after one skipped
    run.
    """
    query = (
        sa.select(ProgramInvitationRow.response_state)
        .where(ProgramInvitationRow.program_id == program_id)
        .where(ProgramInvitationRow.user_id == user_id)
        .where(ProgramInvitationRow.delivery_state.notin_(("skipped", "canceled")))
        .order_by(ProgramInvitationRow.created_at.desc())
        .limit(count)
    )
    if last_inbound_at is not None:
        query = query.where(ProgramInvitationRow.created_at > _naive_utc(last_inbound_at))
    async with session_factory() as db:
        recent = (await db.execute(query)).scalars().all()
    return len(recent) == count and all(state == "no_reply_by_deadline" for state in recent)


def _naive_utc(value: datetime) -> datetime:
    """Compare stamps whatever the driver hands back (aware or naive UTC)."""
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)
