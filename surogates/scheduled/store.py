from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from surogates.db.models import ScheduledSession as ScheduledSessionRow
from surogates.scheduled.models import ScheduledSession
from surogates.scheduled.schedule import (
    DYNAMIC_LOOP_EXPIRY_DAYS,
    DYNAMIC_LOOP_FALLBACK_DELAY_SECONDS,
    DYNAMIC_LOOP_STALE_RUN_SECONDS,
    DEFAULT_LOOP_EXPIRY_DAYS,
    ParsedSchedule,
    clamp_dynamic_loop_delay,
    parse_dynamic_loop_schedule,
    parse_schedule,
)


def _validate_principal(
    user_id: UUID | None,
    service_account_id: UUID | None,
    *,
    caller: str,
) -> None:
    """Reject calls that don't specify exactly one owning principal.

    The DB CHECK constraint ``ck_scheduled_sessions_one_principal``
    enforces the invariant, but rejecting up front surfaces a clean
    ``ValueError`` instead of an ``IntegrityError`` at commit time.
    """
    if (user_id is None) == (service_account_id is None):
        raise ValueError(
            f"{caller} requires exactly one of user_id / service_account_id"
        )


def _owner_filter_clauses(user_id: UUID | None, service_account_id: UUID | None):
    """SQL predicate matching the row owned by the given principal.

    Caller is responsible for invoking :func:`_validate_principal`
    first; this helper just emits the WHERE fragment.
    """
    if user_id is not None:
        return ScheduledSessionRow.user_id == user_id
    return ScheduledSessionRow.service_account_id == service_account_id


class ScheduledSessionStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def create(
        self,
        *,
        org_id: UUID,
        agent_id: str,
        name: str,
        prompt: str,
        schedule: ParsedSchedule,
        source: str,
        created_from_session_id: UUID | None,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
        repeat_limit: int | None = None,
        next_run_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> ScheduledSession:
        _validate_principal(
            user_id, service_account_id, caller="ScheduledSessionStore.create",
        )
        row = ScheduledSessionRow(
            org_id=org_id,
            user_id=user_id,
            service_account_id=service_account_id,
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
        agent_id: str,
        prompt: str,
        schedule: ParsedSchedule,
        created_from_session_id: UUID | None,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
    ) -> ScheduledSession:
        return await self.create(
            org_id=org_id,
            user_id=user_id,
            service_account_id=service_account_id,
            agent_id=agent_id,
            name=f"Loop: {prompt[:60]}",
            prompt=prompt,
            schedule=schedule,
            source="loop",
            created_from_session_id=created_from_session_id,
            expires_at=_utcnow() + timedelta(days=DEFAULT_LOOP_EXPIRY_DAYS),
        )

    async def create_dynamic_loop(
        self,
        *,
        org_id: UUID,
        agent_id: str,
        prompt: str,
        created_from_session_id: UUID | None,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
        schedule: ParsedSchedule | None = None,
    ) -> ScheduledSession:
        now = _utcnow()
        return await self.create(
            org_id=org_id,
            user_id=user_id,
            service_account_id=service_account_id,
            agent_id=agent_id,
            name=f"Loop: {prompt[:60]}",
            prompt=prompt,
            schedule=schedule or parse_dynamic_loop_schedule(),
            source="loop",
            created_from_session_id=created_from_session_id,
            next_run_at=now,
            expires_at=now + timedelta(days=DYNAMIC_LOOP_EXPIRY_DAYS),
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
        agent_id: str,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
        include_inactive: bool = False,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ScheduledSession]:
        _validate_principal(
            user_id, service_account_id,
            caller="ScheduledSessionStore.list_for_user",
        )
        stmt = (
            select(ScheduledSessionRow)
            .where(
                ScheduledSessionRow.org_id == org_id,
                _owner_filter_clauses(user_id, service_account_id),
                ScheduledSessionRow.agent_id == agent_id,
            )
            .order_by(ScheduledSessionRow.created_at.desc())
        )
        if status and status != "all":
            stmt = stmt.where(ScheduledSessionRow.status == status)
        elif not include_inactive:
            stmt = stmt.where(ScheduledSessionRow.status == "active")
        if offset > 0:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        async with self._sf() as db:
            result = await db.execute(stmt)
            rows = result.scalars().all()
        return [ScheduledSession.model_validate(row) for row in rows]

    async def pause(
        self,
        *,
        org_id: UUID,
        agent_id: str,
        schedule_id: UUID,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
    ) -> bool:
        return await self._set_status(
            org_id=org_id,
            user_id=user_id,
            service_account_id=service_account_id,
            agent_id=agent_id,
            schedule_id=schedule_id,
            status="paused",
        )

    async def resume(
        self,
        *,
        org_id: UUID,
        agent_id: str,
        schedule_id: UUID,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
    ) -> bool:
        _validate_principal(
            user_id, service_account_id,
            caller="ScheduledSessionStore.resume",
        )
        schedule = await self.get(schedule_id)
        if schedule.schedule.get("kind") == "dynamic_loop":
            next_run_at = _utcnow()
        else:
            next_run_at = _parsed_schedule(schedule).next_after(_utcnow())
        async with self._sf() as db:
            result = await db.execute(
                update(ScheduledSessionRow)
                .where(
                    ScheduledSessionRow.id == schedule_id,
                    ScheduledSessionRow.org_id == org_id,
                    _owner_filter_clauses(user_id, service_account_id),
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
        agent_id: str,
        schedule_id: UUID,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
    ) -> bool:
        _validate_principal(
            user_id, service_account_id,
            caller="ScheduledSessionStore.delete",
        )
        async with self._sf() as db:
            result = await db.execute(
                delete(ScheduledSessionRow).where(
                    ScheduledSessionRow.id == schedule_id,
                    ScheduledSessionRow.org_id == org_id,
                    _owner_filter_clauses(user_id, service_account_id),
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
        agent_id: str,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
    ) -> bool:
        return await self.delete(
            org_id=org_id,
            user_id=user_id,
            service_account_id=service_account_id,
            agent_id=agent_id,
            schedule_id=schedule_id,
        )

    async def run_now(
        self,
        *,
        org_id: UUID,
        agent_id: str,
        schedule_id: UUID,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
    ) -> bool:
        _validate_principal(
            user_id, service_account_id,
            caller="ScheduledSessionStore.run_now",
        )
        async with self._sf() as db:
            result = await db.execute(
                update(ScheduledSessionRow)
                .where(
                    ScheduledSessionRow.id == schedule_id,
                    ScheduledSessionRow.org_id == org_id,
                    _owner_filter_clauses(user_id, service_account_id),
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

    async def find_due_across_tenants(
        self,
        *,
        worker_id: str,
        limit: int,
        lease_seconds: int = 120,
    ) -> list[ScheduledSession]:
        """Multi-tenant variant of :meth:`claim_due` for platform ticker.

        Drops the ``agent_id`` filter so a single call returns
        due rows across ALL tenants in one DB round-trip.  The
        existing claim-lock (``locked_by`` / ``locked_until``)
        and the partial index on ``(agent_id, status,
        next_run_at)`` apply unchanged -- ``SELECT ... FOR
        UPDATE SKIP LOCKED`` ensures concurrent platform
        replicas (if leader election briefly leaks) don't fight
        for the same rows.

        Returns at most ``limit`` rows ordered by
        ``next_run_at ASC`` so the oldest due work fires first.
        """
        query = text(
            """
            WITH due AS (
                SELECT id
                FROM scheduled_sessions
                WHERE status = 'active'
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
        is_dynamic = schedule.schedule.get("kind") == "dynamic_loop"
        next_run_at = (
            None
            if completed or is_dynamic
            else _parsed_schedule(schedule).next_after(now)
        )
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
        is_dynamic = schedule.schedule.get("kind") == "dynamic_loop"
        next_run_at = (
            now + timedelta(seconds=DYNAMIC_LOOP_FALLBACK_DELAY_SECONDS)
            if is_dynamic
            else _parsed_schedule(schedule).next_after(now)
        )
        values: dict[str, Any] = {
            "last_error": error[:2000],
            "locked_by": None,
            "locked_until": None,
            "next_run_at": next_run_at,
            "updated_at": func.now(),
        }
        if is_dynamic:
            values["schedule"] = {
                **schedule.schedule,
                "last_delay_seconds": DYNAMIC_LOOP_FALLBACK_DELAY_SECONDS,
                "last_delay_reason": "Run creation failed; using fallback delay.",
                "last_delay_set_at": now.isoformat(),
            }
        async with self._sf() as db:
            await db.execute(
                update(ScheduledSessionRow)
                .where(ScheduledSessionRow.id == schedule.id)
                .values(**values)
            )
            await db.commit()

    async def mark_dynamic_run_finished(
        self,
        *,
        schedule_id: UUID,
        org_id: UUID,
        agent_id: str,
        session_id: UUID,
        delay_seconds: int,
        reason: str,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
        completed: bool = False,
    ) -> bool:
        _validate_principal(
            user_id, service_account_id,
            caller="ScheduledSessionStore.mark_dynamic_run_finished",
        )
        delay = clamp_dynamic_loop_delay(delay_seconds)
        now = _utcnow()
        async with self._sf() as db:
            row = await db.get(ScheduledSessionRow, schedule_id, with_for_update=True)
            if row is None:
                return False
            if user_id is not None:
                principal_match = row.user_id == user_id
            else:
                principal_match = row.service_account_id == service_account_id
            if (
                row.org_id != org_id
                or not principal_match
                or row.agent_id != agent_id
                or row.last_session_id != session_id
                or row.schedule.get("kind") != "dynamic_loop"
            ):
                return False

            # The caller (loop_wait) can declare the loop done; otherwise the
            # only way a dynamic loop terminates is by reaching ``expires_at``.
            done = completed or (row.expires_at is not None and row.expires_at <= now)
            schedule_data = {
                **row.schedule,
                "last_delay_seconds": delay,
                "last_delay_reason": reason.strip()[:500] or "No reason provided.",
                "last_delay_set_at": now.isoformat(),
            }
            row.schedule = schedule_data
            row.next_run_at = None if done else now + timedelta(seconds=delay)
            row.status = "completed" if done else "active"
            row.locked_by = None
            row.locked_until = None
            row.updated_at = now
            await db.commit()
            return True

    async def mark_loop_completed(
        self,
        *,
        schedule_id: UUID,
        org_id: UUID,
        agent_id: str,
        session_id: UUID,
        reason: str,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
    ) -> bool:
        """Mark a scheduled loop completed at the request of its running session.

        Used by the ``loop_complete`` tool so a fixed-cron ``/loop`` child
        can self-terminate when its prompt's stop condition is met.  The
        tenant/principal/agent/session tuple is enforced — only the
        currently-running session can complete its own schedule, mirroring
        the guard in ``mark_dynamic_run_finished``.

        Returns ``True`` on success, ``False`` when the row doesn't exist
        or the tuple doesn't match.
        """
        _validate_principal(
            user_id, service_account_id,
            caller="ScheduledSessionStore.mark_loop_completed",
        )
        now = _utcnow()
        async with self._sf() as db:
            row = await db.get(ScheduledSessionRow, schedule_id, with_for_update=True)
            if row is None:
                return False
            if user_id is not None:
                principal_match = row.user_id == user_id
            else:
                principal_match = row.service_account_id == service_account_id
            if (
                row.org_id != org_id
                or not principal_match
                or row.agent_id != agent_id
                or row.last_session_id != session_id
            ):
                return False
            schedule_data = {
                **row.schedule,
                "last_completed_reason": reason.strip()[:500] or "No reason provided.",
                "last_completed_at": now.isoformat(),
            }
            row.schedule = schedule_data
            row.next_run_at = None
            row.status = "completed"
            row.locked_by = None
            row.locked_until = None
            row.updated_at = now
            await db.commit()
            return True

    async def recover_stalled_dynamic_loops(
        self,
        *,
        agent_id: str,
        stale_seconds: int = DYNAMIC_LOOP_STALE_RUN_SECONDS,
        limit: int = 100,
    ) -> list[ScheduledSession]:
        now = _utcnow()
        reason = (
            "Previous dynamic loop run stalled; using the fallback delay."
        )
        query = text(
            """
            WITH stalled AS (
                SELECT s.id
                FROM scheduled_sessions s
                JOIN sessions run_session ON run_session.id = s.last_session_id
                WHERE s.agent_id = :agent_id
                  AND s.status = 'active'
                  AND s.next_run_at IS NULL
                  AND s.last_session_id IS NOT NULL
                  AND s.schedule->>'kind' = 'dynamic_loop'
                  AND (s.expires_at IS NULL OR s.expires_at > now())
                  AND run_session.status IN ('completed', 'failed', 'idle', 'archived')
                ORDER BY s.last_run_at ASC NULLS FIRST
                LIMIT :limit
                FOR UPDATE OF s SKIP LOCKED
            )
            UPDATE scheduled_sessions s
            SET next_run_at = now() + make_interval(secs => :delay_seconds),
                schedule = s.schedule || jsonb_build_object(
                    'last_delay_seconds', :delay_seconds,
                    'last_delay_reason', CAST(:reason AS text),
                    'last_delay_set_at', CAST(:set_at AS text)
                ),
                last_error = CAST(:reason AS text),
                locked_by = NULL,
                locked_until = NULL,
                updated_at = now()
            FROM stalled
            WHERE s.id = stalled.id
            RETURNING s.*
            """
        )
        async with self._sf() as db:
            result = await db.execute(
                query,
                {
                    "agent_id": agent_id,
                    "stale_seconds": int(stale_seconds),
                    "limit": int(limit),
                    "delay_seconds": DYNAMIC_LOOP_FALLBACK_DELAY_SECONDS,
                    "reason": reason,
                    "set_at": now.isoformat(),
                },
            )
            rows = [ScheduledSession.model_validate(dict(row._mapping)) for row in result]
            await db.commit()
        return rows

    async def find_retryable_stalled_dynamic_loop_runs(
        self,
        *,
        agent_id: str,
        stale_seconds: int = DYNAMIC_LOOP_STALE_RUN_SECONDS,
        limit: int = 100,
    ) -> list[ScheduledSession]:
        query = text(
            """
            SELECT s.*
            FROM scheduled_sessions s
            JOIN sessions run_session ON run_session.id = s.last_session_id
            LEFT JOIN session_leases active_lease
                ON active_lease.session_id = run_session.id
               AND active_lease.expires_at > now()
            WHERE s.agent_id = :agent_id
              AND s.status = 'active'
              AND s.next_run_at IS NULL
              AND s.last_session_id IS NOT NULL
              AND s.schedule->>'kind' = 'dynamic_loop'
              AND (s.expires_at IS NULL OR s.expires_at > now())
              AND run_session.status = 'active'
              AND active_lease.session_id IS NULL
              AND run_session.updated_at
                    < now() - make_interval(secs => :stale_seconds)
            ORDER BY s.last_run_at ASC NULLS FIRST
            LIMIT :limit
            """
        )
        async with self._sf() as db:
            result = await db.execute(
                query,
                {
                    "agent_id": agent_id,
                    "stale_seconds": int(stale_seconds),
                    "limit": int(limit),
                },
            )
            rows = [ScheduledSession.model_validate(dict(row._mapping)) for row in result]
        return rows

    async def _set_status(
        self,
        *,
        org_id: UUID,
        agent_id: str,
        schedule_id: UUID,
        status: str,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
    ) -> bool:
        _validate_principal(
            user_id, service_account_id,
            caller="ScheduledSessionStore._set_status",
        )
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
                    _owner_filter_clauses(user_id, service_account_id),
                    ScheduledSessionRow.agent_id == agent_id,
                )
                .values(**values)
            )
            await db.commit()
        return bool(result.rowcount)


def _schedule_to_dict(schedule: ParsedSchedule) -> dict[str, str]:
    data = {
        "kind": schedule.kind,
        "display": schedule.display,
        "timezone_name": schedule.timezone_name,
    }
    if schedule.cron is not None:
        data["cron"] = schedule.cron
    if schedule.adjusted_from is not None:
        data["adjusted_from"] = schedule.adjusted_from
    return data


def _parsed_schedule(schedule: ScheduledSession) -> ParsedSchedule:
    data = schedule.schedule
    if data.get("kind") == "dynamic_loop":
        return parse_dynamic_loop_schedule(
            timezone_name=str(data.get("timezone_name") or schedule.timezone),
        )
    return parse_schedule(
        str(data["cron"]),
        timezone_name=str(data.get("timezone_name") or schedule.timezone),
    )


def _default_name(prompt: str) -> str:
    return prompt.strip()[:80] or "Scheduled session"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
