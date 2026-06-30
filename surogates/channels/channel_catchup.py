"""Boot-time catch-up: replay Slack messages missed while the platform was down.

On channels-process startup, for each Slack app the bot is provisioned in, list
the bot's conversations and replay any human messages newer than the last one we
processed (the watermark) through the normal inbound pipeline. Bounded by
``BackfillLimits``; silent; best-effort; safe to run on every restart.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from surogates.session.events import EventType

logger = logging.getLogger(__name__)


_WATERMARK_SQL = text("""
    SELECT COALESCE(
        e.data #>> '{source,ts}',
        to_char(EXTRACT(EPOCH FROM e.created_at AT TIME ZONE 'UTC'), 'FM9999999990.000000')
    ) AS watermark
    FROM events e
    JOIN sessions s ON s.id = e.session_id
    WHERE e.org_id = :org_id
      AND s.agent_id = :agent_id
      AND e.type = :event_type
      AND e.data #>> '{source,platform}' = 'slack'
      AND e.data #>> '{source,api_app_id}' = :api_app_id
      AND e.data #>> '{source,chat_id}' = :chat_id
      AND NOT (e.data ? 'synthetic')
    ORDER BY watermark DESC
    LIMIT 1
""")


def _watermark_from(source_ts: str | None, created_at: datetime | None) -> str | None:
    """Pick the catch-up watermark for a conversation.

    Prefers the exact stored Slack ``source.ts`` string; falls back to the latest
    event's ``created_at`` rendered as a Slack-style ts (compatibility bridge for
    events stored before ``source.ts`` existed); ``None`` when we have never
    processed the conversation (first-run guard).
    """
    if source_ts:
        return source_ts
    if created_at is not None:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return f"{created_at.timestamp():.6f}"
    return None


async def latest_catchup_watermark(
    session_factory: Any,
    *,
    org_id: Any,
    agent_id: str,
    api_app_id: str,
    chat_id: str,
) -> str | None:
    """Latest Slack ts we have processed for (org, agent, Slack app, conversation).

    Slack ``ts`` is compared/selected as a string (fixed ``seconds.microseconds``
    shape) — never converted to float. Returns ``None`` when there is no
    non-synthetic ``USER_MESSAGE`` for the conversation.
    """
    async with session_factory() as db:
        result = await db.execute(
            _WATERMARK_SQL,
            {
                "org_id": org_id,
                "agent_id": agent_id,
                "event_type": EventType.USER_MESSAGE.value,
                "api_app_id": api_app_id,
                "chat_id": chat_id,
            },
        )
        watermark = result.scalar_one_or_none()
    return str(watermark) if watermark is not None else None
