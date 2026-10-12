"""Tell the care team when someone has stopped answering.

A Program can set ``alert_after_missed: N``.  When a person's last ``N``
openers from that Program all went unanswered, every alert recipient of the
agent is told, by phone.  Silence is the one signal the person cannot send
themselves: someone who fainted does not reply "I fainted".

Runs right after the deadline sweep, which is what turns an unanswered opener
into ``no_reply_by_deadline``.  Each alert is keyed on the invitation that
completed the streak, so a re-scan never alerts twice.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa

from surogates.db.models import (
    ProgramInvitationRow,
    ProgramScheduleRow,
    User,
)
from surogates.followup import store as followup_store

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_MISSED_TEXT", "alert_missed_checkins", "render_missed_text"]

#: How far back a newly unanswered opener is looked at: long enough that a
#: ticker down for a few hours still alerts, short enough that a pass does
#: not re-read the same silence all day.
LOOKBACK = timedelta(hours=6)

#: Used when the Program carries no wording of its own.  Placeholders:
#: ``{name}``, ``{phone}``, ``{count}``, ``{program}``.
DEFAULT_MISSED_TEXT = (
    "{name} ({phone}) has not answered the last {count} messages of "
    "“{program}”."
)


def render_missed_text(template: str | None, **values: Any) -> str:
    """The alert text, never failing on a malformed operator template."""
    return followup_store.render_text(template, DEFAULT_MISSED_TEXT, **values)


def _threshold(config: dict) -> int | None:
    raw = (config or {}).get("alert_after_missed")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return None
    return raw


async def alert_missed_checkins(session_factory: Any, *, now: datetime) -> int:
    """Queue alerts for every streak of unanswered openers.  Returns how many."""
    async with session_factory() as db:
        # Only Programs that ask for it: everything else is never read.
        watched = {
            pid: cfg for pid, cfg in (
                await db.execute(
                    sa.select(ProgramScheduleRow.program_id, ProgramScheduleRow.config)
                    .where(ProgramScheduleRow.active.is_(True))
                )
            ).all()
            if _threshold(cfg) is not None
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

    settings_cache: dict[tuple, Any] = {}
    queued = 0
    for inv in newest.values():
        config = watched[inv.program_id]
        try:
            queued += await _alert_if_streak(
                session_factory, inv, config, _threshold(config), settings_cache,
            )
        except Exception:  # noqa: BLE001 — one person, not the pass
            logger.exception(
                "[followup] could not check the missed streak for invitation %s",
                inv.id,
            )
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
        user = await db.get(User, inv.user_id)

    key = (inv.org_id, inv.agent_id)
    if key not in settings_cache:
        settings_cache[key] = await followup_store.get_settings(
            session_factory, org_id=inv.org_id, agent_id=inv.agent_id,
        )
    text = render_missed_text(
        config.get("missed_alert_text"),
        name=(user.display_name if user else "") or "?",
        phone=f"+{inv.platform_user_id}" if inv.platform_user_id else "?",
        count=threshold,
        program=str(config.get("name") or "check-in"),
    )
    queued, _why_not = await followup_store.notify_care_team(
        session_factory, settings_cache[key], org_id=inv.org_id,
        agent_id=inv.agent_id, kind="alert", text=text,
        created_by="missed-checkins", subject_user_id=inv.user_id,
        dedupe_prefix=f"missed:{inv.id}",
        fallback_sender=str(config.get("channel_identifier") or "") or None,
    )

    async with session_factory() as db:
        row = await db.get(ProgramInvitationRow, inv.id)
        if queued:
            row.escalation_state = "raised"
            row.reason = f"No answer to {threshold} openers in a row; care team alerted"
        else:
            # Visible, never silent: the streak happened and nobody could
            # be told.  Marked so the history says so and the pass moves on.
            row.escalation_state = "failed"
            row.reason = (
                f"No answer to {threshold} openers in a row; no care-team "
                "phone is configured to tell"
            )
        await db.commit()
    return queued
