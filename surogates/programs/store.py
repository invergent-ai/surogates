"""Store for ``program_schedules`` — one row per active check-in Program.

Reuses the platform-ticker claim/lock pattern (``locked_by`` / ``locked_until``
+ ``SELECT FOR UPDATE SKIP LOCKED``), the same one ``surogates.ambient.store``
uses.  Portable across SQLite (tests) and Postgres (prod): the claim is
SQLAlchemy Core so it runs on both, and the SKIP LOCKED optimisation is applied
opportunistically.

Times here are **naive UTC**, matching what ``cadence.next_occurrences``
returns.  Mixing naive and aware datetimes across this boundary is the kind of
bug that only shows up an hour after a DST change.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import async_sessionmaker

from surogates.db.models import ProgramScheduleRow
from surogates.programs.cadence import next_occurrences

__all__ = ["ProgramSchedule", "ProgramScheduleStore"]

#: Floor on how far a failed tick pushes its next run.  A Program whose
#: cadence yields no computable instant must still come back eventually:
#: parking it on ``next_run_at=NULL`` would drop it out of the ticker for good,
#: because reconcile only rewrites the clock when the cadence itself changes.
_MIN_FAILURE_BACKOFF_SECONDS: int = 300

#: The cadence fields.  ``ensure`` rewrites ``next_run_at`` only when one of
#: these moves — see its docstring for why that matters.
_CADENCE_KEYS = ("weekdays", "times_local", "timezone")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ProgramSchedule(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    program_id: UUID
    org_id: UUID
    agent_id: str
    config: dict = {}
    active: bool = True
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    locked_by: str | None = None
    locked_until: datetime | None = None


class ProgramScheduleStore:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory

    async def ensure(
        self,
        *,
        program_id: UUID,
        org_id: UUID,
        agent_id: str,
        config: dict[str, Any],
        next_run_at: datetime | None,
    ) -> ProgramSchedule:
        """Create or refresh the schedule mirroring one active ops Program.

        ``next_run_at`` is written only when the row is new, the cadence
        changed, or the Program is coming back from paused.  Reconcile runs on
        every ``program_changed`` publish and on a timer, so unconditionally
        resetting the clock would push a due Program forward on every
        unrelated save — forever, and it would never fire.

        Resuming is the third case because a Program paused past its slot
        still carries that stale instant: without recomputing, resuming it on
        Wednesday would immediately fire Monday's missed check-in at every
        patient.
        """
        async with self._sf() as db:
            row = (
                await db.execute(
                    sa.select(ProgramScheduleRow)
                    .where(ProgramScheduleRow.program_id == program_id)
                )
            ).scalar_one_or_none()

            if row is None:
                row = ProgramScheduleRow(
                    program_id=program_id,
                    org_id=org_id,
                    agent_id=agent_id,
                    config=config,
                    active=True,
                    next_run_at=next_run_at,
                )
                db.add(row)
            else:
                old = row.config or {}
                cadence_changed = any(
                    old.get(k) != config.get(k) for k in _CADENCE_KEYS
                )
                resuming = not row.active
                row.org_id = org_id
                row.agent_id = agent_id
                row.config = config
                row.active = True
                if cadence_changed or resuming:
                    row.next_run_at = next_run_at

            await db.commit()
            await db.refresh(row)
            return ProgramSchedule.model_validate(row)

    async def get(self, program_id: UUID) -> ProgramSchedule | None:
        async with self._sf() as db:
            row = (
                await db.execute(
                    sa.select(ProgramScheduleRow)
                    .where(ProgramScheduleRow.program_id == program_id)
                )
            ).scalar_one_or_none()
            return (
                ProgramSchedule.model_validate(row) if row is not None else None
            )

    async def claim_due(
        self, *, worker_id: str, limit: int, lease_seconds: int = 120,
    ) -> list[ProgramSchedule]:
        """Lease every active, due, unlocked schedule, up to *limit*."""
        now = _utcnow()
        async with self._sf() as db:
            rows = (
                await db.execute(
                    sa.select(ProgramScheduleRow)
                    .where(ProgramScheduleRow.active.is_(True))
                    .where(ProgramScheduleRow.next_run_at.isnot(None))
                    .where(ProgramScheduleRow.next_run_at <= now)
                    .where(
                        sa.or_(
                            ProgramScheduleRow.locked_until.is_(None),
                            ProgramScheduleRow.locked_until <= now,
                        )
                    )
                    .order_by(ProgramScheduleRow.next_run_at.asc())
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()

            claimed: list[ProgramSchedule] = []
            for row in rows:
                row.locked_by = worker_id
                row.locked_until = now + timedelta(seconds=lease_seconds)
                claimed.append(ProgramSchedule.model_validate(row))
            await db.commit()
            return claimed

    async def mark_fired(
        self, schedule: ProgramSchedule, *, next_run_at: datetime | None,
    ) -> None:
        now = _utcnow()
        async with self._sf() as db:
            await db.execute(
                sa.update(ProgramScheduleRow)
                .where(ProgramScheduleRow.id == schedule.id)
                .values(
                    last_run_at=now,
                    next_run_at=next_run_at,
                    locked_by=None,
                    locked_until=None,
                )
            )
            await db.commit()

    async def mark_failed(self, schedule: ProgramSchedule) -> None:
        """Advance a failed tick's clock and drop its lock.

        Without this the row keeps ``next_run_at`` in the past while still
        naming a worker that has already given up, so ``claim_due`` re-claims
        it the moment the lease lapses — forever, at lease frequency, with no
        backoff.  A failed tick waits a full cadence, like a skipped one.
        """
        now = _utcnow()
        config = schedule.config or {}
        upcoming = next_occurrences(
            now,
            weekdays=config.get("weekdays") or [],
            times_local=config.get("times_local") or [],
            timezone=config.get("timezone") or "UTC",
            count=1,
        )
        # No computable instant (an empty or unsatisfiable cadence) still gets
        # a clock, so the row stays in the ticker's world and a later reconcile
        # can correct it.
        floor = now + timedelta(seconds=_MIN_FAILURE_BACKOFF_SECONDS)
        retry_at = max(upcoming[0], floor) if upcoming else floor

        async with self._sf() as db:
            await db.execute(
                sa.update(ProgramScheduleRow)
                .where(ProgramScheduleRow.id == schedule.id)
                .values(
                    next_run_at=retry_at, locked_by=None, locked_until=None,
                )
            )
            await db.commit()

    async def deactivate(self, program_id: UUID) -> None:
        """Stop a Program firing, and release any lease it holds."""
        async with self._sf() as db:
            await db.execute(
                sa.update(ProgramScheduleRow)
                .where(ProgramScheduleRow.program_id == program_id)
                .values(active=False, locked_by=None, locked_until=None)
            )
            await db.commit()

    async def deactivate_missing(self, keep: set[UUID]) -> None:
        """Stop every active Program that is no longer in the ops projection.

        This is the whole reason reconcile exists: a Program paused or deleted
        in ops would otherwise keep messaging patients on schedule.
        """
        async with self._sf() as db:
            stmt = (
                sa.update(ProgramScheduleRow)
                .where(ProgramScheduleRow.active.is_(True))
                .values(active=False, locked_by=None, locked_until=None)
            )
            if keep:
                stmt = stmt.where(ProgramScheduleRow.program_id.notin_(keep))
            await db.execute(stmt)
            await db.commit()
