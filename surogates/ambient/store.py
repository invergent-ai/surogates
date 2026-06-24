"""Store for ambient_schedules — one ambient-review schedule per channel.

Reuses the platform-ticker claim/lock pattern (locked_by/locked_until) without
the scheduled_sessions principal constraint.  Portable across SQLite (tests)
and Postgres (prod): the claim uses SQLAlchemy Core so it runs on both; the
SKIP LOCKED optimisation is Postgres-only and applied opportunistically.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import async_sessionmaker

from surogates.db.models import AmbientScheduleRow

__all__ = ["AmbientSchedule", "AmbientScheduleStore"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AmbientSchedule(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    org_id: UUID
    agent_id: str
    platform: str
    channel_id: str
    source_session_id: UUID | None = None
    ambient_session_id: UUID | None = None
    cadence_seconds: int
    status: str
    next_run_at: datetime | None = None
    locked_by: str | None = None
    locked_until: datetime | None = None
    config: dict = {}


class AmbientScheduleStore:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory

    async def ensure(
        self,
        *,
        org_id: UUID,
        agent_id: str,
        platform: str,
        channel_id: str,
        source_session_id: UUID | None,
        cadence_seconds: int,
        config: dict[str, Any],
    ) -> AmbientSchedule:
        async with self._sf() as db:
            row = (
                await db.execute(
                    sa.select(AmbientScheduleRow)
                    .where(AmbientScheduleRow.agent_id == agent_id)
                    .where(AmbientScheduleRow.platform == platform)
                    .where(AmbientScheduleRow.channel_id == channel_id)
                )
            ).scalar_one_or_none()
            if row is None:
                row = AmbientScheduleRow(
                    org_id=org_id,
                    agent_id=agent_id,
                    platform=platform,
                    channel_id=channel_id,
                    source_session_id=source_session_id,
                    cadence_seconds=cadence_seconds,
                    status="active",
                    next_run_at=_utcnow() + timedelta(seconds=cadence_seconds),
                    config=config,
                )
                db.add(row)
            else:
                # No-op reconcile is the common case (every channel wake calls
                # ensure): skip the UPDATE entirely when nothing changed so a
                # busy channel doesn't write to this table on every mention.
                unchanged = (
                    row.status == "active"
                    and row.cadence_seconds == cadence_seconds
                    and row.config == config
                    and (
                        source_session_id is None
                        or row.source_session_id == source_session_id
                    )
                )
                if unchanged:
                    return AmbientSchedule.model_validate(row)
                row.cadence_seconds = cadence_seconds
                row.status = "active"
                if source_session_id is not None:
                    row.source_session_id = source_session_id
                if config:
                    row.config = config
            await db.commit()
            await db.refresh(row)
            return AmbientSchedule.model_validate(row)

    async def get(
        self, *, agent_id: str, platform: str, channel_id: str,
    ) -> AmbientSchedule | None:
        async with self._sf() as db:
            row = (
                await db.execute(
                    sa.select(AmbientScheduleRow)
                    .where(AmbientScheduleRow.agent_id == agent_id)
                    .where(AmbientScheduleRow.platform == platform)
                    .where(AmbientScheduleRow.channel_id == channel_id)
                )
            ).scalar_one_or_none()
            return AmbientSchedule.model_validate(row) if row is not None else None

    async def claim_due(
        self, *, worker_id: str, limit: int, lease_seconds: int = 120,
    ) -> list[AmbientSchedule]:
        now = _utcnow()
        async with self._sf() as db:
            rows = (
                await db.execute(
                    sa.select(AmbientScheduleRow)
                    .where(AmbientScheduleRow.status == "active")
                    .where(AmbientScheduleRow.next_run_at.isnot(None))
                    .where(AmbientScheduleRow.next_run_at <= now)
                    .where(
                        sa.or_(
                            AmbientScheduleRow.locked_until.is_(None),
                            AmbientScheduleRow.locked_until <= now,
                        )
                    )
                    .order_by(AmbientScheduleRow.next_run_at.asc())
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()
            claimed: list[AmbientSchedule] = []
            for row in rows:
                row.locked_by = worker_id
                row.locked_until = now + timedelta(seconds=lease_seconds)
                claimed.append(AmbientSchedule.model_validate(row))
            await db.commit()
            return claimed

    async def mark_fired(
        self, schedule: AmbientSchedule, *, ambient_session_id: UUID,
    ) -> None:
        now = _utcnow()
        async with self._sf() as db:
            await db.execute(
                sa.update(AmbientScheduleRow)
                .where(AmbientScheduleRow.id == schedule.id)
                .values(
                    ambient_session_id=ambient_session_id,
                    next_run_at=now + timedelta(seconds=schedule.cadence_seconds),
                    locked_by=None,
                    locked_until=None,
                    updated_at=now,
                )
            )
            await db.commit()

    async def deactivate(self, schedule_id: UUID) -> None:
        async with self._sf() as db:
            await db.execute(
                sa.update(AmbientScheduleRow)
                .where(AmbientScheduleRow.id == schedule_id)
                .values(status="paused", locked_by=None, locked_until=None)
            )
            await db.commit()
