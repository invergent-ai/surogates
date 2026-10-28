from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, literal_column, or_, select, text, update
from sqlalchemy.orm import aliased
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from surogates.channels.memory_boundary import CASE_BOUNDARY_PREFIX
from surogates.db.models import Event as EventRow
from surogates.db.models import InboxItem
from surogates.db.models import Session as SessionRow
from surogates.db.models import ScheduledSession as ScheduledSessionRow
from surogates.db.models import ServiceAccount as ServiceAccountRow
from surogates.db.models import User as UserRow
from surogates.scheduled.models import ScheduledSession
from surogates.session.events import EventType
from surogates.tenant.auth.service_account import KIND_AGENT_PRINCIPAL
from surogates.tools.owner_scope import OPS_CHAT_SA_PREFIX
from surogates.scheduled.schedule import (
    DEFAULT_LOOP_EXPIRY_DAYS,
    DYNAMIC_LOOP_EXPIRY_DAYS,
    DYNAMIC_LOOP_FALLBACK_DELAY_SECONDS,
    DYNAMIC_LOOP_STALE_RUN_SECONDS,
    ParsedSchedule,
    clamp_dynamic_loop_delay,
    duration_words,
    kind_sql,
    parse_dynamic_loop_schedule,
    row_kind,
    schedule_from_row,
)

_KIND = kind_sql()

#: Failed starts of a one-off before it ends ``failed``.
MAX_ONCE_ATTEMPTS = 3
#: Failed starts in a row of any other schedule before it ends ``failed``.
MAX_CONSECUTIVE_FAILURES = 10


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
        context: dict[str, Any] | None = None,
        max_run_seconds: int | None = None,
    ) -> ScheduledSession:
        _validate_principal(
            user_id, service_account_id, caller="ScheduledSessionStore.create",
        )
        now = _utcnow()
        schedule = schedule.anchored(now)
        if next_run_at is None:
            next_run_at = now if schedule.is_dynamic else schedule.next_after(now)
        if next_run_at is None:
            raise ValueError("That time has already passed.")
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
            kind=schedule.kind,
            cadence=schedule.cadence(),
            context=context or {},
            max_run_seconds=max_run_seconds,
            status="active",
            source=source,
            repeat_limit=repeat_limit,
            next_run_at=next_run_at,
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
        created_from_session_id: UUID | None,
        schedule: ParsedSchedule | None = None,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
        context: dict[str, Any] | None = None,
    ) -> ScheduledSession:
        """A ``/loop``: on *schedule*, or with none, one whose runs pick
        their own next delay.  A loop typed in passing should not outlive
        the work it was for: a fixed one expires after
        :data:`DEFAULT_LOOP_EXPIRY_DAYS`, a dynamic one, bounded by nothing
        but the model, after :data:`DYNAMIC_LOOP_EXPIRY_DAYS`.  A schedule
        meant to last is made with ``cron_create`` or in Studio."""
        schedule = schedule or parse_dynamic_loop_schedule()
        days = DYNAMIC_LOOP_EXPIRY_DAYS if schedule.is_dynamic else DEFAULT_LOOP_EXPIRY_DAYS
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
            expires_at=_utcnow() + timedelta(days=days),
            context=context or {},
        )

    async def get(self, schedule_id: UUID) -> ScheduledSession:
        async with self._sf() as db:
            row = await db.get(ScheduledSessionRow, schedule_id)
        if row is None:
            raise KeyError(f"scheduled session {schedule_id} not found")
        return ScheduledSession.model_validate(row)

    async def get_for(
        self,
        schedule_id: UUID,
        *,
        org_id: UUID,
        agent_id: str,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
        hide_cases: bool = False,
        case_session_id: UUID | None = None,
        own_only: bool = False,
    ) -> ScheduledSession | None:
        """*schedule_id* when the principal may see it (:func:`_scope`), else
        None.  *hide_cases*: as :func:`_not_a_case_schedule`."""
        _validate_principal(user_id, service_account_id, caller="ScheduledSessionStore.get_for")
        stmt = select(ScheduledSessionRow).where(
            ScheduledSessionRow.id == schedule_id,
            ScheduledSessionRow.org_id == org_id,
            ScheduledSessionRow.agent_id == agent_id,
            _scope(user_id, service_account_id, own_only=own_only),
        )
        if hide_cases:
            stmt = stmt.where(_not_a_case_schedule(case_session_id))
        async with self._sf() as db:
            row = (await db.execute(stmt)).scalar_one_or_none()
        return None if row is None else ScheduledSession.model_validate(row)

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
        created_from_session_id: UUID | None = None,
        hide_cases: bool = False,
        case_session_id: UUID | None = None,
        own_only: bool = False,
    ) -> list[ScheduledSession]:
        """The schedules the principal may see (:func:`_scope`), newest first.
        *hide_cases*: as :func:`_not_a_case_schedule`.

        *status* is one status, a comma list, or ``all``; without it only
        active ones, or every one with *include_inactive*.
        """
        _validate_principal(
            user_id, service_account_id,
            caller="ScheduledSessionStore.list_for_user",
        )
        stmt = (
            select(ScheduledSessionRow)
            .where(
                ScheduledSessionRow.org_id == org_id,
                _scope(user_id, service_account_id, own_only=own_only),
                ScheduledSessionRow.agent_id == agent_id,
            )
            .order_by(ScheduledSessionRow.created_at.desc())
        )
        if status and status != "all":
            stmt = stmt.where(ScheduledSessionRow.status.in_(
                [part.strip() for part in status.split(",") if part.strip()]
            ))
        elif not status and not include_inactive:
            stmt = stmt.where(ScheduledSessionRow.status == "active")
        if created_from_session_id is not None:
            stmt = stmt.where(ScheduledSessionRow.created_from_session_id == created_from_session_id)
        if hide_cases:
            stmt = stmt.where(_not_a_case_schedule(case_session_id))
        if offset > 0:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        async with self._sf() as db:
            result = await db.execute(stmt)
            rows = result.scalars().all()
        return [ScheduledSession.model_validate(row) for row in rows]

    async def apply(
        self,
        verb: str,
        *,
        org_id: UUID,
        agent_id: str,
        schedule_id: UUID,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
        ended_by: str | None = None,
    ) -> tuple[bool, ScheduledSession | None]:
        """``pause``, ``resume``, ``cancel`` or ``run_now`` a schedule: whether
        it changed, and the schedule as it now is (None when the principal
        may not see it)."""
        scope = {"org_id": org_id, "agent_id": agent_id,
                 "user_id": user_id, "service_account_id": service_account_id}
        extra = {"ended_by": ended_by} if verb == "cancel" else {}
        changed = await getattr(self, verb)(schedule_id=schedule_id, **scope, **extra)
        if changed is not None:
            return True, changed
        return False, await self.get_for(schedule_id, **scope)

    async def pause(
        self,
        *,
        org_id: UUID,
        agent_id: str,
        schedule_id: UUID,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
    ) -> ScheduledSession | None:
        """Stop an active schedule from starting runs until resumed."""
        return await self._transition(
            org_id=org_id, agent_id=agent_id, schedule_id=schedule_id,
            user_id=user_id, service_account_id=service_account_id,
            from_statuses=("active",),
            values={"status": "paused", "paused_at": func.now(), "next_run_at": None},
        )

    async def resume(
        self,
        *,
        org_id: UUID,
        agent_id: str,
        schedule_id: UUID,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
    ) -> ScheduledSession | None:
        """Start a paused schedule again from its next run after now: a
        missed one-off runs now."""
        schedule = await self.get_for(
            schedule_id, org_id=org_id, agent_id=agent_id,
            user_id=user_id, service_account_id=service_account_id,
        )
        if schedule is None or schedule.status != "paused":
            return None
        return await self._transition(
            org_id=org_id, agent_id=agent_id, schedule_id=schedule_id,
            user_id=user_id, service_account_id=service_account_id,
            from_statuses=("paused",),
            values={
                "status": "active", "paused_at": None,
                "next_run_at": _planned_run(schedule, _utcnow()),
            },
        )

    async def cancel(
        self,
        *,
        org_id: UUID,
        agent_id: str,
        schedule_id: UUID,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
        ended_by: str | None = None,
    ) -> ScheduledSession | None:
        """End an active or paused schedule for good, keeping its row.

        Its run still going is ended by the ticker's sweep
        (:meth:`end_runs_of_cancelled`).
        """
        return await self._transition(
            org_id=org_id, agent_id=agent_id, schedule_id=schedule_id,
            user_id=user_id, service_account_id=service_account_id,
            from_statuses=("active", "paused"),
            values={
                "status": "cancelled", "ended_reason": "cancelled", "ended_by": ended_by,
                "next_run_at": None,
            },
        )

    async def update(
        self,
        *,
        org_id: UUID,
        agent_id: str,
        schedule_id: UUID,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
        name: str | None = None,
        prompt: str | None = None,
        schedule: ParsedSchedule | None = None,
        context: dict[str, Any] | None = None,
        max_run_seconds: int | None = None,
    ) -> ScheduledSession | None:
        """Change an active or paused schedule; the changed one, or None
        when there is none the principal may change.  A new cadence of an
        active schedule plans its next run from it."""
        current = await self.get_for(
            schedule_id, org_id=org_id, agent_id=agent_id,
            user_id=user_id, service_account_id=service_account_id,
        )
        if current is None or current.status not in ("active", "paused"):
            return None
        values: dict[str, Any] = {}
        if name is not None:
            values["name"] = name.strip() or current.name
        if prompt is not None:
            values["prompt"] = prompt
        if context is not None:
            values["context"] = context
        if max_run_seconds is not None:
            values["max_run_seconds"] = max_run_seconds or None
        if schedule is not None:
            was = _schedule_of(current)
            if schedule.is_dynamic != was.is_dynamic:
                raise ValueError("A dynamic loop cannot become a fixed schedule, or the reverse.")
            now = _utcnow()
            # The same interval keeps its rhythm when only the zone or the text changes.
            schedule = schedule.anchored(
                was.anchor if (was.kind, was.seconds) == (schedule.kind, schedule.seconds) and was.anchor else now
            )
            values.update(
                schedule=_schedule_to_dict(schedule), schedule_display=schedule.display,
                timezone=schedule.timezone_name, kind=schedule.kind, cadence=schedule.cadence(),
            )
            if schedule.kind != was.kind:
                # A one-off runs once; the run limit of what it was is not the new one's.
                values["repeat_limit"] = 1 if schedule.kind == "once" else None
            if current.status == "active" and not schedule.is_dynamic:
                next_run_at = schedule.next_after(now)
                if next_run_at is None:
                    raise ValueError("That time has already passed.")
                values["next_run_at"] = next_run_at
        if not values:
            return current
        return await self._transition(
            org_id=org_id, agent_id=agent_id, schedule_id=schedule_id,
            user_id=user_id, service_account_id=service_account_id,
            from_statuses=(current.status,), values=values,
        )

    async def run_now(
        self,
        *,
        org_id: UUID,
        agent_id: str,
        schedule_id: UUID,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
    ) -> ScheduledSession | None:
        return await self._transition(
            org_id=org_id, agent_id=agent_id, schedule_id=schedule_id,
            user_id=user_id, service_account_id=service_account_id,
            from_statuses=("active",), values={"next_run_at": _utcnow()},
            unlocked=False,
        )

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

    async def expire_active_schedules(
        self,
        *,
        agent_id: str | None = None,
        limit: int = 100,
    ) -> list[ScheduledSession]:
        """Transition schedules past ``expires_at`` from ``active`` to
        ``completed``.

        The claim queries (:meth:`claim_due` / :meth:`find_due_across_tenants`)
        exclude rows with ``expires_at <= now()``, and the expiry check in
        :meth:`mark_run_created` only runs on a successful claim. A schedule
        whose ``expires_at`` falls between its last run and its next due instant
        therefore expires *unclaimed* — it can never be claimed again, so the
        completion code never runs and the row is stuck ``active`` forever
        (still ``next_run_at``-due, never firing). This independent sweep reaps
        those zombies regardless of claimability.

        ``agent_id=None`` sweeps across all tenants (platform ticker); passing
        an ``agent_id`` scopes to one tenant. Returns the swept rows.
        """
        agent_filter = "" if agent_id is None else "AND s.agent_id = :agent_id"
        query = text(
            f"""
            WITH expired AS (
                SELECT s.id
                FROM scheduled_sessions s
                WHERE s.status = 'active'
                  {agent_filter}
                  AND s.expires_at IS NOT NULL
                  AND s.expires_at <= now()
                ORDER BY s.expires_at ASC
                LIMIT :limit
                FOR UPDATE OF s SKIP LOCKED
            )
            UPDATE scheduled_sessions s
            SET status = 'completed',
                ended_reason = 'expired',
                next_run_at = NULL,
                locked_by = NULL,
                locked_until = NULL,
                updated_at = now()
            FROM expired
            WHERE s.id = expired.id
            RETURNING s.*
            """
        )
        params: dict[str, Any] = {"limit": int(limit)}
        if agent_id is not None:
            params["agent_id"] = agent_id
        async with self._sf() as db:
            result = await db.execute(query, params)
            rows = [ScheduledSession.model_validate(dict(row._mapping)) for row in result]
            await db.commit()
        return rows

    async def mark_run_created(
        self,
        schedule: ScheduledSession,
        *,
        session_id: UUID,
    ) -> None:
        """Record the run *session_id* started for *schedule* and plan the next.

        The row is re-read under its lock: a person who paused or cancelled
        it since the claim keeps their status, and only the run is recorded.
        """
        now = _utcnow()
        async with self._sf() as db:
            row = await db.get(ScheduledSessionRow, schedule.id, with_for_update=True)
            if row is None:
                return
            # A run that ended well ends the failure streak; a failed one is
            # counted where it failed (mark_run_timed_out).
            if row.last_session_id is None or await db.scalar(
                select(SessionRow.status).where(SessionRow.id == row.last_session_id)
            ) != "failed":
                row.consecutive_failures = 0
            row.run_count = (row.run_count or 0) + 1
            row.last_run_at = now
            row.last_session_id = session_id
            row.last_error = None
            row.locked_by = None
            row.locked_until = None
            row.updated_at = now
            if row.status == "active":
                current = ScheduledSession.model_validate(row)
                cadence = _schedule_of(current)
                ended = None
                if row.repeat_limit is not None and row.run_count >= row.repeat_limit:
                    ended = "limit"
                elif row.expires_at is not None and row.expires_at <= now:
                    ended = "expired"
                # A one-off has run once, even when made to run early.
                done_once = cadence.kind == "once"
                next_run_at = None if ended or done_once or cadence.is_dynamic else cadence.next_after(now)
                if ended is None and next_run_at is None and not cadence.is_dynamic:
                    ended = "done"
                row.next_run_at = next_run_at
                if ended is not None:
                    _end(row, ended)
            await db.commit()

    async def mark_run_failed(
        self,
        schedule: ScheduledSession,
        *,
        error: str,
    ) -> None:
        """Release a claimed schedule whose run could not be started.

        A recurring schedule moves on to its next run; a one-off or a
        dynamic loop tries again after a growing delay.  After
        :data:`MAX_ONCE_ATTEMPTS` failures of a one-off, or
        :data:`MAX_CONSECUTIVE_FAILURES` in a row of any other, it ends
        ``failed``.  Only the lock is released when a person paused or
        cancelled it meanwhile.
        """
        now = _utcnow()
        async with self._sf() as db:
            row = await db.get(ScheduledSessionRow, schedule.id, with_for_update=True)
            if row is None:
                return
            row.locked_by = None
            row.locked_until = None
            row.last_error = error[:2000]
            row.updated_at = now
            if row.status == "active":
                failures = (row.consecutive_failures or 0) + 1
                row.consecutive_failures = failures
                cadence = _schedule_of(ScheduledSession.model_validate(row))
                limit = MAX_ONCE_ATTEMPTS if cadence.kind == "once" else MAX_CONSECUTIVE_FAILURES
                if failures >= limit:
                    _end(row, "failed")
                elif cadence.kind == "once" or cadence.is_dynamic:
                    delay = min(DYNAMIC_LOOP_FALLBACK_DELAY_SECONDS, 60 * 2 ** (failures - 1))
                    row.next_run_at = now + timedelta(seconds=delay)
                    if cadence.is_dynamic:
                        row.schedule = {
                            **row.schedule,
                            "last_delay_seconds": delay,
                            "last_delay_reason": "Run creation failed; trying again.",
                            "last_delay_set_at": now.isoformat(),
                        }
                else:
                    row.next_run_at = cadence.next_after(now)
            await db.commit()

    async def mark_run_timed_out(self, run_id: UUID, *, limit_seconds: int) -> None:
        """Count a run stopped at its time limit as a failure of its schedule:
        said on ``last_error``, and after :data:`MAX_CONSECUTIVE_FAILURES` in
        a row an active schedule ends ``failed``."""
        now = _utcnow()
        async with self._sf() as db:
            row = (await db.execute(
                select(ScheduledSessionRow)
                .where(ScheduledSessionRow.last_session_id == run_id)
                .with_for_update()
            )).scalar_one_or_none()
            if row is None:
                return
            row.last_error = (
                f"The last run was stopped after {duration_words(limit_seconds)}, its time limit, "
                "before it finished."
            )
            row.updated_at = now
            if row.status == "active":
                row.consecutive_failures = (row.consecutive_failures or 0) + 1
                if row.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    _end(row, "failed")
            await db.commit()

    async def mark_run_skipped(self, schedule: ScheduledSession, *, reason: str) -> None:
        """Pass over a due run (its previous run is still going) and plan the
        next one, saying why on ``last_error``."""
        now = _utcnow()
        async with self._sf() as db:
            row = await db.get(ScheduledSessionRow, schedule.id, with_for_update=True)
            if row is None:
                return
            row.locked_by = None
            row.locked_until = None
            row.last_error = reason[:2000]
            row.updated_at = now
            if row.status == "active":
                cadence = _schedule_of(ScheduledSession.model_validate(row))
                row.next_run_at = (
                    now + timedelta(seconds=DYNAMIC_LOOP_FALLBACK_DELAY_SECONDS)
                    if cadence.is_dynamic or cadence.kind == "once"
                    else cadence.next_after(now)
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
        """The run *session_id* chose when its dynamic loop runs next, or
        that it is done.  Not once the loop is no longer active: a person's
        pause or cancel, and the run's own ``completed``, are final."""
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
                or row.status != "active"
                or row_kind(row) != "dynamic"
            ):
                return False

            expired = row.expires_at is not None and row.expires_at <= now
            row.schedule = {
                **row.schedule,
                "last_delay_seconds": delay,
                "last_delay_reason": reason.strip()[:500] or "No reason provided.",
                "last_delay_set_at": now.isoformat(),
            }
            row.next_run_at = now + timedelta(seconds=delay)
            if completed or expired:
                _end(row, "done" if completed else "expired")
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

        Used by the ``loop_complete`` tool so a fixed ``/loop`` run can end
        its schedule when its prompt's stop condition is met.  Only the
        currently running session can, and only while the schedule is
        active.  Whether it did.
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
                or row.status != "active"
            ):
                return False
            row.schedule = {
                **row.schedule,
                "last_completed_reason": reason.strip()[:500] or "No reason provided.",
                "last_completed_at": now.isoformat(),
            }
            _end(row, "done")
            row.locked_by = None
            row.locked_until = None
            row.updated_at = now
            await db.commit()
            return True

    async def plan_unscheduled(self, *, limit: int = 100) -> list[ScheduledSession]:
        """Give active rows with no next run one: rows the control plane
        made or resumed that way.  A dynamic loop waiting on its run has no
        next run either, and is left to that run."""
        now = _utcnow()
        async with self._sf() as db:
            rows = (await db.execute(
                select(ScheduledSessionRow)
                .where(
                    ScheduledSessionRow.status == "active",
                    ScheduledSessionRow.next_run_at.is_(None),
                    ~(
                        (literal_column(kind_sql("scheduled_sessions")) == "dynamic")
                        & ScheduledSessionRow.last_session_id.is_not(None)
                    ),
                )
                .order_by(ScheduledSessionRow.updated_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )).scalars().all()
            planned = []
            for row in rows:
                current = ScheduledSession.model_validate(row)
                try:
                    row.next_run_at = _planned_run(current, now)
                except ValueError as exc:
                    _end(row, "failed")
                    row.last_error = f"Cannot plan this schedule: {exc}"[:2000]
                row.updated_at = now
                planned.append(row)
            await db.commit()
            return [ScheduledSession.model_validate(row) for row in planned]

    async def claim_unannounced_endings(self, *, limit: int = 100) -> list[ScheduledSession]:
        """Schedules that ended in the last day with no person ending them
        (expired, ran out of runs, failed) and whose chat was not told yet;
        each is returned once, for the caller to tell it."""
        now = _utcnow()
        async with self._sf() as db:
            rows = (await db.execute(
                select(ScheduledSessionRow)
                .where(
                    ScheduledSessionRow.status.in_(("completed", "failed")),
                    ScheduledSessionRow.ended_reason.in_(("expired", "limit", "failed")),
                    ScheduledSessionRow.ended_announced_at.is_(None),
                    ScheduledSessionRow.updated_at > now - timedelta(days=1),
                )
                .order_by(ScheduledSessionRow.updated_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )).scalars().all()
            for row in rows:
                row.ended_announced_at = now
            ended = [ScheduledSession.model_validate(row) for row in rows]
            await db.commit()
            return ended

    async def end_runs_of_cancelled(
        self, *, limit: int = 100, schedule_id: UUID | None = None,
    ) -> list[UUID]:
        """Runs still going of schedules cancelled in the last day (or of
        the one *schedule_id*): their unanswered questions are retired here,
        and the caller stops them.
        """
        query = text(
            """
            SELECT DISTINCT r.id
            FROM scheduled_sessions s
            JOIN sessions r
              ON (r.parent_id = s.created_from_session_id OR r.id = s.last_session_id)
            WHERE s.status = 'cancelled'
              AND s.updated_at > now() - interval '1 day'
              AND (CAST(:schedule_id AS uuid) IS NULL OR s.id = CAST(:schedule_id AS uuid))
              AND r.channel = 'scheduled'
              AND r.status = 'active'
              AND r.config->>'scheduled_session_id' = s.id::text
            LIMIT :limit
            """
        )
        params = {"limit": int(limit), "schedule_id": str(schedule_id) if schedule_id else None}
        async with self._sf() as db:
            ids = [row[0] for row in await db.execute(query, params)]
            if ids:
                await db.execute(
                    update(InboxItem)
                    .where(
                        InboxItem.session_id.in_(ids),
                        InboxItem.kind == "input_required",
                        InboxItem.status == "pending",
                    )
                    .values(status="expired", updated_at=func.now())
                )
                await db.commit()
        return ids

    async def owner_names(
        self, schedules: list[ScheduledSession],
    ) -> tuple[dict[UUID, str], dict[UUID, str]]:
        """``(user display names, service account names)`` of the schedules' owners."""
        user_ids = {s.user_id for s in schedules if s.user_id}
        sa_ids = {s.service_account_id for s in schedules if s.service_account_id}
        async with self._sf() as db:
            users = dict((await db.execute(
                select(UserRow.id, UserRow.display_name).where(UserRow.id.in_(user_ids))
            )).all()) if user_ids else {}
            accounts = dict((await db.execute(
                select(ServiceAccountRow.id, ServiceAccountRow.name).where(ServiceAccountRow.id.in_(sa_ids))
            )).all()) if sa_ids else {}
        return users, accounts

    async def list_runs(
        self, schedule_id: UUID, *, org_id: UUID, agent_id: str, limit: int = 20,
    ) -> list[dict[str, Any]]:
        """The sessions a schedule started, newest first, a failed one with
        why it failed in words."""
        stmt = (
            select(SessionRow.id, SessionRow.status, SessionRow.title,
                   SessionRow.created_at, SessionRow.updated_at)
            .where(
                SessionRow.org_id == org_id,
                SessionRow.agent_id == agent_id,
                SessionRow.channel == "scheduled",
                SessionRow.config["scheduled_session_id"].astext == str(schedule_id),
            )
            .order_by(SessionRow.created_at.desc())
            .limit(limit)
        )
        async with self._sf() as db:
            runs = [dict(row._mapping) for row in await db.execute(stmt)]
            failed = [run["id"] for run in runs if run["status"] == "failed"]
            errors: dict[UUID, str] = {}
            if failed:
                failures = await db.execute(
                    select(EventRow.session_id, EventRow.data)
                    .where(EventRow.session_id.in_(failed), EventRow.type == EventType.SESSION_FAIL.value)
                    .order_by(EventRow.id)
                )
                for session_id, data in failures.all():
                    if words := _failure_words(data or {}):
                        errors[session_id] = words[:500]
        return [{**run, "error": errors.get(run["id"])} for run in runs]

    async def recover_stalled_dynamic_loops(
        self,
        *,
        agent_id: str | None = None,
        stale_seconds: int = DYNAMIC_LOOP_STALE_RUN_SECONDS,
        limit: int = 100,
    ) -> list[ScheduledSession]:
        """Reschedule dynamic loops whose run reached a terminal state
        without rescheduling (the run never called ``loop_wait`` ->
        ``mark_dynamic_run_finished``), using the fallback delay.

        ``agent_id=None`` sweeps across all tenants (platform ticker);
        passing an ``agent_id`` scopes to one tenant.
        """
        now = _utcnow()
        reason = (
            "Previous dynamic loop run stalled; using the fallback delay."
        )
        agent_filter = "" if agent_id is None else "AND s.agent_id = :agent_id"
        query = text(
            f"""
            WITH stalled AS (
                SELECT s.id
                FROM scheduled_sessions s
                JOIN sessions run_session ON run_session.id = s.last_session_id
                WHERE s.status = 'active'
                  {agent_filter}
                  AND s.next_run_at IS NULL
                  AND s.last_session_id IS NOT NULL
                  AND {_KIND} = 'dynamic'
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
        params: dict[str, Any] = {
            "stale_seconds": int(stale_seconds),
            "limit": int(limit),
            "delay_seconds": DYNAMIC_LOOP_FALLBACK_DELAY_SECONDS,
            "reason": reason,
            "set_at": now.isoformat(),
        }
        if agent_id is not None:
            params["agent_id"] = agent_id
        async with self._sf() as db:
            result = await db.execute(query, params)
            rows = [ScheduledSession.model_validate(dict(row._mapping)) for row in result]
            await db.commit()
        return rows

    async def find_retryable_stalled_dynamic_loop_runs(
        self,
        *,
        agent_id: str | None = None,
        stale_seconds: int = DYNAMIC_LOOP_STALE_RUN_SECONDS,
        limit: int = 100,
    ) -> list[ScheduledSession]:
        """Dynamic loops whose run row is still ``active`` but whose
        worker lease has lapsed and which hasn't progressed for
        ``stale_seconds`` — i.e. the worker died mid-run.  The run is
        re-enqueued (by the caller) rather than rescheduled.

        ``agent_id=None`` sweeps across all tenants.
        """
        agent_filter = "" if agent_id is None else "AND s.agent_id = :agent_id"
        query = text(
            f"""
            SELECT s.*
            FROM scheduled_sessions s
            JOIN sessions run_session ON run_session.id = s.last_session_id
            LEFT JOIN session_leases active_lease
                ON active_lease.session_id = run_session.id
               AND active_lease.expires_at > now()
            WHERE s.status = 'active'
              {agent_filter}
              AND s.next_run_at IS NULL
              AND s.last_session_id IS NOT NULL
              AND {_KIND} = 'dynamic'
              AND (s.expires_at IS NULL OR s.expires_at > now())
              AND run_session.status = 'active'
              AND active_lease.session_id IS NULL
              AND run_session.updated_at
                    < now() - make_interval(secs => :stale_seconds)
            ORDER BY s.last_run_at ASC NULLS FIRST
            LIMIT :limit
            """
        )
        params: dict[str, Any] = {
            "stale_seconds": int(stale_seconds),
            "limit": int(limit),
        }
        if agent_id is not None:
            params["agent_id"] = agent_id
        async with self._sf() as db:
            result = await db.execute(query, params)
            rows = [ScheduledSession.model_validate(dict(row._mapping)) for row in result]
        return rows

    async def _transition(
        self,
        *,
        org_id: UUID,
        agent_id: str,
        schedule_id: UUID,
        from_statuses: tuple[str, ...],
        values: dict[str, Any],
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
        unlocked: bool = True,
    ) -> ScheduledSession | None:
        """Write *values* to the schedule the principal may change, only from
        *from_statuses*; the changed row, or None.  *unlocked* releases a claim."""
        _validate_principal(
            user_id, service_account_id,
            caller="ScheduledSessionStore._transition",
        )
        values = {**values, "updated_at": func.now()}
        if unlocked:
            values.update(locked_by=None, locked_until=None)
        async with self._sf() as db:
            row = (await db.execute(
                update(ScheduledSessionRow)
                .where(
                    ScheduledSessionRow.id == schedule_id,
                    ScheduledSessionRow.org_id == org_id,
                    _scope(user_id, service_account_id),
                    ScheduledSessionRow.agent_id == agent_id,
                    ScheduledSessionRow.status.in_(from_statuses),
                )
                .values(**values)
                .returning(ScheduledSessionRow)
            )).scalar_one_or_none()
            changed = None if row is None else ScheduledSession.model_validate(row)
            await db.commit()
        return changed


def _schedule_to_dict(schedule: ParsedSchedule) -> dict[str, str]:
    """``schedule``, as readers older than ``kind``/``cadence`` read it."""
    data = {
        "kind": schedule.legacy_kind,
        "display": schedule.display,
        "timezone_name": schedule.timezone_name,
    }
    if schedule.cron is not None:
        data["cron"] = schedule.cron
    return data


def _schedule_of(schedule: ScheduledSession) -> ParsedSchedule:
    return schedule_from_row(schedule)


def _planned_run(schedule: ScheduledSession, now: datetime) -> datetime:
    """When an active *schedule* with no next run runs: a dynamic loop or
    a one-off whose time has passed runs now."""
    cadence = _schedule_of(schedule)
    if cadence.is_dynamic:
        return now
    return cadence.next_after(now) or now


def _not_a_case_schedule(case_session_id: UUID | None):
    """Not a schedule a Keel case's session made or ran last: case data is
    Keel's.  The case session's own token (*case_session_id*) still sees
    the ones it is part of."""
    case = aliased(SessionRow)
    is_case = case.config["memory_boundary"].astext.startswith(CASE_BOUNDARY_PREFIX, autoescape=True)
    if case_session_id is not None:
        is_case = is_case & (case.id != case_session_id)
    return ~exists().where(
        or_(case.id == ScheduledSessionRow.created_from_session_id,
            case.id == ScheduledSessionRow.last_session_id),
        is_case,
    )


def _scope(user_id: UUID | None, service_account_id: UUID | None, *, own_only: bool = False):
    """The schedules a principal sees and manages.

    A person sees their own.  A Studio operator's chat account and the
    agent's own principal work for the project and see all of the agent's:
    a co-owner manages a schedule another made, and a channel user's
    schedule is visible to the people who run the agent.  Any other
    service account (an API key, an integration) sees only its own, as
    does any caller asking with *own_only*.
    """
    if user_id is not None:
        return ScheduledSessionRow.user_id == user_id
    if own_only:
        return ScheduledSessionRow.service_account_id == service_account_id
    works_for_the_project = (
        select(ServiceAccountRow.id)
        .where(
            ServiceAccountRow.id == service_account_id,
            ServiceAccountRow.org_id == ScheduledSessionRow.org_id,
            or_(
                ServiceAccountRow.name.startswith(OPS_CHAT_SA_PREFIX),
                (ServiceAccountRow.kind == KIND_AGENT_PRINCIPAL)
                & (ServiceAccountRow.agent_id == ScheduledSessionRow.agent_id),
            ),
        )
        .exists()
    )
    return or_(ScheduledSessionRow.service_account_id == service_account_id, works_for_the_project)


def _end(row: ScheduledSessionRow, reason: str) -> None:
    """End *row* on its own: ``failed``, or completed as *reason* says."""
    row.status = "failed" if reason == "failed" else "completed"
    row.ended_reason = reason
    row.next_run_at = None


def _failure_words(data: dict) -> str | None:
    """Why a scheduled run failed, in words a person reads."""
    reason = data.get("error_title") or data.get("error") or data.get("reason")
    if reason == "run_time_limit":
        seconds = int(data.get("limit_seconds") or 0)
        limit = (f" of {seconds // 3600} h" if seconds and seconds % 3600 == 0
                 else f" of {seconds // 60} min" if seconds else "")
        return f"Stopped at its time limit{limit} before it finished"
    if reason == "schedule_cancelled":
        return "Stopped because the schedule was cancelled"
    return str(reason) if reason else None


def _default_name(prompt: str) -> str:
    return prompt.strip()[:80] or "Scheduled session"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
