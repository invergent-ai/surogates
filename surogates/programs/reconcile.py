"""Mirror ops' active Programs into the runtime's schedule table.

Runs on a ``program_changed`` publish and, as a backstop, on a slow interval:
a missed publish must not leave a paused Program messaging patients.

The whole projected row is stored as the schedule's ``config`` — roster
included — so a tick never has to call back into ops to find out who to
message.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from surogates.programs.cadence import next_occurrences

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def reconcile_programs(store: Any, *, projected: list[dict]) -> None:
    """Make the runtime's schedules match *projected* exactly.

    Each row is handled on its own.  Nothing validates the projection before
    it arrives here, and a single malformed field — a timezone this pod's
    tzdata lacks, ``"9"`` for a time, a non-UUID id — used to raise out of
    the loop.  That skipped every Program after the bad one **and** skipped
    ``deactivate_missing``, so one bad row meant a Program the operator had
    paused kept messaging patients indefinitely, fleet-wide, every tick.

    A row that cannot be parsed is logged and left out of ``seen``, which
    deactivates its schedule.  A Program we cannot understand must not fire.
    """
    seen: set[uuid.UUID] = set()
    now = _utcnow()

    for row in projected:
        try:
            program_id = uuid.UUID(str(row["id"]))
            org_id = uuid.UUID(str(row["org_id"]))
            agent_id = str(row["agent_id"])
            upcoming = next_occurrences(
                now,
                weekdays=list(row.get("weekdays") or []),
                times_local=list(row.get("times_local") or []),
                timezone=str(row.get("timezone") or "UTC"),
                count=1,
            )
        except Exception:  # noqa: BLE001 — one bad row must not stop the rest
            logger.exception(
                "[programs] skipping unparseable projected program %r",
                row.get("id") if isinstance(row, dict) else row,
            )
            continue

        try:
            await store.ensure(
                program_id=program_id,
                org_id=org_id,
                agent_id=agent_id,
                config=row,
                next_run_at=upcoming[0] if upcoming else None,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "[programs] could not mirror program %s; leaving its schedule "
                "as it was", program_id,
            )
            # Keep it in `seen`: a transient store error is not a reason to
            # deactivate a Program that is genuinely still active in ops.
        seen.add(program_id)

    await store.deactivate_missing(seen)
