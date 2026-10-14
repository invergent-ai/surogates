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
    ProgramInvitationRow,
    ProgramScheduleRow,
)
from surogates.channels.identity import readable_phone
from surogates.followup import store as followup_store
from surogates.followup.eligibility import as_utc
from surogates.followup.outreach import describe_people

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
                .where(ProgramInvitationRow.deadline_at >= now - LOOKBACK)
                .order_by(ProgramInvitationRow.created_at)
            )
        ).scalars().all()

    # Only the newest unanswered opener of each person in each Program can
    # complete a streak; the older ones were judged when they were newest.
    newest: dict[tuple, Any] = {}
    for inv in candidates:
        newest[(inv.program_id, inv.user_id)] = inv
    # A streak is judged once.  The lookback keeps an opener a candidate for
    # hours, and without this every pass (each minute) would re-run the
    # streak query for every one of them.  Kept per process and pruned with
    # the lookback; a restart re-judges once, which is harmless because
    # alerts are deduplicated.
    for inv_id, deadline in list(_JUDGED.items()):
        if deadline < now - LOOKBACK:
            del _JUDGED[inv_id]

    settings_cache: dict[tuple, Any] = {}
    queued = 0
    for inv in newest.values():
        if inv.id in _JUDGED:
            continue
        config = watched[inv.program_id]
        try:
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


async def _alert_if_streak(
    session_factory: Any, inv: Any, config: dict, threshold: int, settings_cache: dict,
) -> int:
    async with session_factory() as db:
        recent = (
            await db.execute(
                sa.select(
                    ProgramInvitationRow.response_state,
                    ProgramInvitationRow.escalation_state,
                )
                .where(ProgramInvitationRow.program_id == inv.program_id)
                .where(ProgramInvitationRow.user_id == inv.user_id)
                .where(ProgramInvitationRow.created_at <= inv.created_at)
                # Skips are not silence: the person was never asked.
                .where(ProgramInvitationRow.delivery_state.notin_(("skipped", "canceled")))
                .order_by(ProgramInvitationRow.created_at.desc())
                .limit(threshold)
            )
        ).all()
        if len(recent) < threshold or any(r != "no_reply_by_deadline" for r, _ in recent):
            return 0
        # A streak already reported does not count again: the next alert needs
        # another full run of unanswered openers, not one more on top of it.
        if any(e != "none" for _, e in recent[1:]):
            return 0

    key = (inv.org_id, inv.agent_id)
    if key not in settings_cache:
        settings_cache[key] = await followup_store.get_settings(
            session_factory, org_id=inv.org_id, agent_id=inv.agent_id,
        )
    [person] = (await describe_people(
        session_factory, org_id=inv.org_id, agent_id=inv.agent_id, user_ids=[inv.user_id],
    )).values()
    text = render_missed_text(
        config.get("missed_alert_text"),
        name=person.name or "?",
        phone=readable_phone(inv.platform_user_id) if inv.platform_user_id else "?",
        count=threshold,
        program=str(config.get("name") or "check-in"),
    )
    queued, _why_not = await followup_store.notify_team(
        session_factory, settings_cache[key], org_id=inv.org_id,
        agent_id=inv.agent_id, kind="alert", text=text,
        created_by="missed-checkins", subject_user_id=inv.user_id,
        dedupe_prefix=f"missed:{inv.id}",
        # The Program's own sender, on its own channel: a Slack Program's
        # team id is no WhatsApp sender.
        fallback_senders={
            str(config.get("channel") or "whatsapp"): str(config.get("channel_identifier") or ""),
        },
    )

    # Keyed on the invitation, so a pass retried after a crash updates the
    # one record instead of queuing the team a second item.
    await followup_store.record_escalation(
        session_factory, escalation_id=inv.id, org_id=inv.org_id,
        agent_id=inv.agent_id, user_id=inv.user_id, kind="missed_replies",
        reason=text, state="raised" if queued else "failed", platform=inv.platform,
        invitation_id=inv.id, program_id=inv.program_id, session_id=inv.session_id,
    )
    async with session_factory() as db:
        row = await db.get(ProgramInvitationRow, inv.id)
        if queued:
            row.escalation_state = "raised"
            row.reason = f"No answer to {threshold} openers in a row; team alerted"
        else:
            # Visible, never silent: the streak happened and nobody could
            # be told.  Marked so the history says so and the pass moves on.
            row.escalation_state = "failed"
            row.reason = (
                f"No answer to {threshold} openers in a row; nobody on the "
                "team could be told"
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
