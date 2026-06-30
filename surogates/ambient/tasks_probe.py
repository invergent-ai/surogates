"""Surface recently-changed delegated tasks for the ambient prompt.

The DB query and the human-readable summarisation are split so the
formatting logic is unit-testable without a Postgres engine (the ``Task``
model uses Postgres dialect types).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker

from surogates.db.models import Task

_TERMINAL = ("done", "blocked", "failed")


def summarize_tasks(rows: list[Any]) -> list[str]:
    """Human-readable one-liners for tasks in a notable state."""
    out: list[str] = []
    for t in rows:
        goal = (getattr(t, "goal", None) or "task").strip()
        status = getattr(t, "status", "")
        if status == "blocked":
            reason = (getattr(t, "blocked_reason", None) or "").strip()
            out.append(f"Task '{goal}' is blocked" + (f": {reason}" if reason else ""))
        elif status == "failed":
            out.append(f"Task '{goal}' failed")
        else:
            out.append(f"Task '{goal}' is now done")
    return out


async def recent_task_changes(
    session_factory: async_sessionmaker,
    *,
    org_id: UUID,
    source_session_id: UUID,
    since_seconds: int = 86400,
) -> list[str]:
    """Summaries of this session's delegated tasks in a terminal/blocked state."""
    async with session_factory() as db:
        rows = (
            await db.execute(
                sa.select(Task)
                .where(Task.org_id == org_id)
                .where(Task.parent_session_id == source_session_id)
                .where(Task.status.in_(_TERMINAL))
                .order_by(Task.completed_at.desc())
                .limit(20)
            )
        ).scalars().all()
    return summarize_tasks(list(rows))
