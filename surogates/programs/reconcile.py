"""Mirror ops' active Programs into the runtime's schedule table.

Runs on a ``program_changed`` publish and, as a backstop, on a slow interval:
a missed publish must not leave a paused Program messaging patients.

The whole projected row is stored as the schedule's ``config`` — roster
included — so a tick never has to call back into ops to find out who to
message.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from surogates.programs.cadence import next_occurrences


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def reconcile_programs(store: Any, *, projected: list[dict]) -> None:
    """Make the runtime's schedules match *projected* exactly."""
    seen: set[uuid.UUID] = set()
    now = _utcnow()

    for row in projected:
        program_id = uuid.UUID(row["id"])
        seen.add(program_id)
        upcoming = next_occurrences(
            now,
            weekdays=row.get("weekdays") or [],
            times_local=row.get("times_local") or [],
            timezone=row.get("timezone") or "UTC",
            count=1,
        )
        await store.ensure(
            program_id=program_id,
            org_id=uuid.UUID(row["org_id"]),
            agent_id=row["agent_id"],
            config=row,
            next_run_at=upcoming[0] if upcoming else None,
        )

    await store.deactivate_missing(seen)
