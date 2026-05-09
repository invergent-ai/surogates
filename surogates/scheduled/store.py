from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from surogates.db.models import ScheduledSession as ScheduledSessionRow
from surogates.scheduled.models import ScheduledSession
from surogates.scheduled.schedule import (
    DEFAULT_LOOP_EXPIRY_DAYS,
    ParsedSchedule,
    parse_schedule,
)


class ScheduledSessionStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def create(
        self,
        *,
        org_id: UUID,
        user_id: UUID,
        agent_id: str,
        name: str,
        prompt: str,
        schedule: ParsedSchedule,
        source: str,
        created_from_session_id: UUID | None,
        repeat_limit: int | None = None,
        next_run_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> ScheduledSession:
        row = ScheduledSessionRow(
            org_id=org_id,
            user_id=user_id,
            agent_id=agent_id,
            name=name.strip() or _default_name(prompt),
            prompt=prompt,
            schedule=_schedule_to_dict(schedule),
            schedule_display=schedule.display,
            timezone=schedule.timezone_name,
            status="active",
            source=source,
            repeat_limit=repeat_limit,
            next_run_at=next_run_at or schedule.next_after(_utcnow()),
            expires_at=expires_at,
            created_from_session_id=created_from_session_id,
        )
        async with self._sf() as db:
            db.add(row)
            await db.commit()
            await db.refresh(row)
        return ScheduledSession.model_validate(row)

    async def create_loop(
        self,
        *,
        org_id: UUID,
        user_id: UUID,
        agent_id: str,
        prompt: str,
        schedule: ParsedSchedule,
        created_from_session_id: UUID | None,
    ) -> ScheduledSession:
        return await self.create(
            org_id=org_id,
            user_id=user_id,
            agent_id=agent_id,
            name=f"Loop: {prompt[:60]}",
            prompt=prompt,
            schedule=schedule,
            source="loop",
            created_from_session_id=created_from_session_id,
            expires_at=_utcnow() + timedelta(days=DEFAULT_LOOP_EXPIRY_DAYS),
        )

    async def get(self, schedule_id: UUID) -> ScheduledSession:
        async with self._sf() as db:
            row = await db.get(ScheduledSessionRow, schedule_id)
        if row is None:
            raise KeyError(f"scheduled session {schedule_id} not found")
        return ScheduledSession.model_validate(row)

    async def list_for_user(
        self,
        *,
        org_id: UUID,
        user_id: UUID,
        agent_id: str,
        include_inactive: bool = False,
    ) -> list[ScheduledSession]:
        stmt = (
            select(ScheduledSessionRow)
            .where(
                ScheduledSessionRow.org_id == org_id,
                ScheduledSessionRow.user_id == user_id,
                ScheduledSessionRow.agent_id == agent_id,
            )
            .order_by(ScheduledSessionRow.created_at.desc())
        )
        if not include_inactive:
            stmt = stmt.where(ScheduledSessionRow.status == "active")
        async with self._sf() as db:
            result = await db.execute(stmt)
            rows = result.scalars().all()
        return [ScheduledSession.model_validate(row) for row in rows]

    async def pause(
        self,
        *,
        org_id: UUID,
        user_id: UUID,
        agent_id: str,
        schedule_id: UUID,
    ) -> bool:
        return await self._set_status(
            org_id=org_id,
            user_id=user_id,
            agent_id=agent_id,
            schedule_id=schedule_id,
            status="paused",
        )

    async def resume(
        self,
        *,
        org_id: UUID,
        user_id: UUID,
        agent_id: str,
        schedule_id: UUID,
    ) -> bool:
        schedule = await self.get(schedule_id)
        next_run_at = _parsed_schedule(schedule).next_after(_utcnow())
        async with self._sf() as db:
            result = await db.execute(
                update(ScheduledSessionRow)
                .where(
                    ScheduledSessionRow.id == schedule_id,
                    ScheduledSessionRow.org_id == org_id,
                    ScheduledSessionRow.user_id == user_id,
                    ScheduledSessionRow.agent_id == agent_id,
                )
                .values(
                    status="active",
                    next_run_at=next_run_at,
                    locked_by=None,
                    locked_until=None,
                    updated_at=func.now(),
                )
            )
            await db.commit()
        return bool(result.rowcount)

    async def delete(
        self,
        *,
        org_id: UUID,
        user_id: UUID,
        agent_id: str,
        schedule_id: UUID,
    ) -> bool:
        async with self._sf() as db:
            result = await db.execute(
                delete(ScheduledSessionRow).where(
                    ScheduledSessionRow.id == schedule_id,
                    ScheduledSessionRow.org_id == org_id,
                    ScheduledSessionRow.user_id == user_id,
                    ScheduledSessionRow.agent_id == agent_id,
                )
            )
            await db.commit()
        return bool(result.rowcount)

    async def delete_for_user(
        self,
        schedule_id: UUID,
        *,
        org_id: UUID,
        user_id: UUID,
        agent_id: str,
    ) -> bool:
        return await self.delete(
            org_id=org_id,
            user_id=user_id,
            agent_id=agent_id,
            schedule_id=schedule_id,
        )

    async def run_now(
        self,
        *,
        org_id: UUID,
        user_id: UUID,
        agent_id: str,
        schedule_id: UUID,
    ) -> bool:
        async with self._sf() as db:
            result = await db.execute(
                update(ScheduledSessionRow)
                .where(
                    ScheduledSessionRow.id == schedule_id,
                    ScheduledSessionRow.org_id == org_id,
                    ScheduledSessionRow.user_id == user_id,
                    ScheduledSessionRow.agent_id == agent_id,
                    ScheduledSessionRow.status == "active",
                )
                .values(next_run_at=_utcnow(), updated_at=func.now())
            )
            await db.commit()
        return bool(result.rowcount)

    async def claim_due(
        self,
        *,
        agent_id: str,
        worker_id: str,
        limit: int,
        lease_seconds: int = 120,
    ) -> list[ScheduledSession]:
        query = text(
            """
            WITH due AS (
                SELECT id
                FROM scheduled_sessions
                WHERE agent_id = :agent_id
                  AND status = 'active'
                  AND next_run_at IS NOT NULL
                  AND next_run_at <= now()
                  AND (locked_until IS NULL OR locked_until <= now())
                  AND (expires_at IS NULL OR expires_at > now())
                ORDER BY next_run_at ASC
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
            )
            UPDATE scheduled_sessions s
            SET locked_by = :worker_id,
                locked_until = now() + make_interval(secs => :lease_seconds),
                updated_at = now()
            FROM due
            WHERE s.id = due.id
            RETURNING s.*
            """
        )
        async with self._sf() as db:
            result = await db.execute(
                query,
                {
                    "agent_id": agent_id,
                    "worker_id": worker_id,
                    "limit": limit,
                    "lease_seconds": lease_seconds,
                },
            )
            rows = [ScheduledSession.model_validate(dict(row._mapping)) for row in result]
            await db.commit()
        return rows

    async def mark_run_created(
        self,
        schedule: ScheduledSession,
        *,
        session_id: UUID,
    ) -> None:
        now = _utcnow()
        next_count = schedule.run_count + 1
        completed = (
            schedule.repeat_limit is not None
            and next_count >= schedule.repeat_limit
        )
        if schedule.expires_at is not None and schedule.expires_at <= now:
            completed = True
        next_run_at = None if completed else _parsed_schedule(schedule).next_after(now)
        async with self._sf() as db:
            await db.execute(
                update(ScheduledSessionRow)
                .where(ScheduledSessionRow.id == schedule.id)
                .values(
                    status="completed" if completed else "active",
                    run_count=next_count,
                    last_run_at=now,
                    last_session_id=session_id,
                    last_error=None,
                    locked_by=None,
                    locked_until=None,
                    next_run_at=next_run_at,
                    updated_at=func.now(),
                )
            )
            await db.commit()

    async def mark_run_failed(
        self,
        schedule: ScheduledSession,
        *,
        error: str,
    ) -> None:
        now = _utcnow()
        async with self._sf() as db:
            await db.execute(
                update(ScheduledSessionRow)
                .where(ScheduledSessionRow.id == schedule.id)
                .values(
                    last_error=error[:2000],
                    locked_by=None,
                    locked_until=None,
                    next_run_at=_parsed_schedule(schedule).next_after(now),
                    updated_at=func.now(),
                )
            )
            await db.commit()

    async def _set_status(
        self,
        *,
        org_id: UUID,
        user_id: UUID,
        agent_id: str,
        schedule_id: UUID,
        status: str,
    ) -> bool:
        values: dict[str, Any] = {"status": status, "updated_at": func.now()}
        if status != "active":
            values["locked_by"] = None
            values["locked_until"] = None
        async with self._sf() as db:
            result = await db.execute(
                update(ScheduledSessionRow)
                .where(
                    ScheduledSessionRow.id == schedule_id,
                    ScheduledSessionRow.org_id == org_id,
                    ScheduledSessionRow.user_id == user_id,
                    ScheduledSessionRow.agent_id == agent_id,
                )
                .values(**values)
            )
            await db.commit()
        return bool(result.rowcount)


def _schedule_to_dict(schedule: ParsedSchedule) -> dict[str, str]:
    return {
        "kind": schedule.kind,
        "cron": schedule.cron,
        "display": schedule.display,
        "timezone_name": schedule.timezone_name,
    }


def _parsed_schedule(schedule: ScheduledSession) -> ParsedSchedule:
    data = schedule.schedule
    return parse_schedule(
        str(data["cron"]),
        timezone_name=str(data.get("timezone_name") or schedule.timezone),
    )


def _default_name(prompt: str) -> str:
    return prompt.strip()[:80] or "Scheduled session"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
