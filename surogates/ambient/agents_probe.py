"""Surface the agents a session started that recently ended, for the ambient prompt.

The DB query and the human-readable summarisation are split so the
formatting logic is unit-testable without a Postgres engine.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker

from surogates.db.models import Session

#: How an agent's session status reads once its run has ended.
_ENDED = {"completed": "finished", "failed": "failed", "paused": "was stopped"}


def summarize_agents(rows: list[Any]) -> list[str]:
    """Human-readable one-liners for agents that ended: each row's ``config`` and ``status``."""
    out: list[str] = []
    for row in rows:
        dispatch = (getattr(row, "config", None) or {}).get("agent_dispatch") or {}
        task = (dispatch.get("description") or "agent").strip()
        out.append(f"Agent '{task}' {_ENDED.get(getattr(row, 'status', ''), 'ended')}")
    return out


async def recent_agent_changes(
    session_factory: async_sessionmaker,
    *,
    org_id: UUID,
    source_session_id: UUID,
    since_seconds: int = 86400,
) -> list[str]:
    """Summaries of the agents this session started that ended in the last *since_seconds*."""
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=since_seconds)
    async with session_factory() as db:
        rows = (
            await db.execute(
                sa.select(Session.config, Session.status)
                .where(Session.org_id == org_id)
                .where(Session.parent_id == source_session_id)
                .where(Session.status.in_(tuple(_ENDED)))
                .where(Session.updated_at > since)
                .order_by(Session.updated_at.desc())
                .limit(20)
            )
        ).all()
    return summarize_agents([row for row in rows if isinstance((row.config or {}).get("agent_dispatch"), dict)])
