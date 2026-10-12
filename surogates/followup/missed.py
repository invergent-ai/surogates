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
import uuid
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

#: How far back a newly unanswered opener is looked at.  Wider than any
#: deadline, so a sweep that ran late still gets its alert.
LOOKBACK = timedelta(hours=48)

#: Used when the Program carries no wording of its own.  Placeholders:
#: ``{name}``, ``{phone}``, ``{count}``, ``{program}``.
DEFAULT_MISSED_TEXT = (
    "{name} ({phone}) has not answered the last {count} messages of "
    "“{program}”."
)


def render_missed_text(template: str | None, **values: Any) -> str:
    """The alert text, never failing on a malformed operator template."""
    text = (template or "").strip() or DEFAULT_MISSED_TEXT
    try:
        return text.format(**values)
    except (KeyError, IndexError, ValueError):
        # An unknown placeholder or a stray brace in the operator's wording
        # must not cost the alert: fall back to the default sentence.
        logger.warning("[followup] unusable missed-alert text; using the default")
        return DEFAULT_MISSED_TEXT.format(**values)


def _threshold(config: dict) -> int | None:
    raw = (config or {}).get("alert_after_missed")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return None
    return raw


async def alert_missed_checkins(session_factory: Any, *, now: datetime) -> int:
    """Queue alerts for every streak of unanswered openers.  Returns how many."""
    async with session_factory() as db:
        candidates = (
            await db.execute(
                sa.select(ProgramInvitationRow)
                .where(ProgramInvitationRow.response_state == "no_reply_by_deadline")
                .where(ProgramInvitationRow.escalation_state == "none")
                .where(ProgramInvitationRow.deadline_at.isnot(None))
                .where(ProgramInvitationRow.deadline_at >= now - LOOKBACK)
                .order_by(ProgramInvitationRow.created_at)
            )
        ).scalars().all()
        program_ids = {c.program_id for c in candidates}
        configs = {
            pid: cfg for pid, cfg in (
                await db.execute(
                    sa.select(ProgramScheduleRow.program_id, ProgramScheduleRow.config)
                    .where(ProgramScheduleRow.program_id.in_(program_ids))
                )
            ).all()
        } if program_ids else {}

    queued = 0
    for inv in candidates:
        config = configs.get(inv.program_id) or {}
        threshold = _threshold(config)
        if threshold is None:
            continue
        try:
            queued += await _alert_if_streak(session_factory, inv, config, threshold)
        except Exception:  # noqa: BLE001 — one person, not the pass
            logger.exception(
                "[followup] could not check the missed streak for invitation %s",
                inv.id,
            )
    return queued


async def _alert_if_streak(
    session_factory: Any, inv: Any, config: dict, threshold: int,
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
        user = await db.get(User, inv.user_id)
    if len(recent) < threshold or any(r != "no_reply_by_deadline" for r, _ in recent):
        return 0
    # A streak already reported does not count again: the next alert needs
    # another full run of unanswered openers, not one more on top of it.
    if any(e != "none" for _, e in recent[1:]):
        return 0

    settings = await followup_store.get_settings(
        session_factory, org_id=inv.org_id, agent_id=inv.agent_id,
    )
    recipients = list(settings.alert_recipient_user_ids or []) if settings else []
    sender = (settings.channel_identifier if settings else None) or str(
        config.get("channel_identifier") or ""
    )
    text = render_missed_text(
        config.get("missed_alert_text"),
        name=(user.display_name if user else "") or "?",
        phone=f"+{inv.platform_user_id}" if inv.platform_user_id else "?",
        count=threshold,
        program=str(config.get("name") or "check-in"),
    )
    queued = 0
    if recipients and sender:
        for recipient in recipients:
            try:
                recipient_id = uuid.UUID(str(recipient))
            except ValueError:
                continue
            await followup_store.queue_outreach(
                session_factory, org_id=inv.org_id, agent_id=inv.agent_id,
                user_id=recipient_id, platform="whatsapp",
                channel_identifier=sender, kind="alert", text=text,
                template_name=settings.alert_template_name,
                template_language=settings.alert_template_language,
                subject_user_id=inv.user_id, created_by="missed-checkins",
                dedupe_key=f"missed:{inv.id}:{recipient_id}",
            )
            queued += 1

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
