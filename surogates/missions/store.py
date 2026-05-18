"""DB CRUD layer for missions.

Provides a small async interface used by slash command handlers,
evaluator, and REST routes. Wraps the existing async_sessionmaker
pattern used elsewhere in Surogates (see ``surogates.session.store``).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from surogates.db.models import Mission as MissionRow
from surogates.missions.models import Mission, MissionStatus


_TERMINAL_STATUSES: tuple[str, ...] = (
    "satisfied", "blocked", "failed", "cancelled", "max_iterations_reached",
)
_ACTIVE_OR_PAUSED: tuple[str, ...] = ("active", "paused")


class MissionStoreError(Exception):
    """Base for mission store errors."""


class MissionNotFoundError(MissionStoreError):
    """Raised when a mission id is not in the DB."""


class ActiveMissionConflictError(MissionStoreError):
    """Raised when create() would violate the one-active-per-session rule."""


class MissionStore:
    """Async CRUD for the ``missions`` table.

    All methods take an open ``async_sessionmaker``; transactions are
    short-lived per call.
    """

    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory

    async def create(
        self,
        *,
        org_id: UUID,
        session_id: UUID,
        agent_id: str,
        description: str,
        rubric: str,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
        max_iterations: int = 20,
    ) -> UUID:
        """Insert a new mission with status='active'.

        Exactly one of ``user_id`` / ``service_account_id`` must be set —
        the DB CHECK constraint enforces it, but reject up front so the
        error surfaces as a ``ValueError`` instead of an ``IntegrityError``.

        Rejects with :class:`ActiveMissionConflictError` if any mission
        with ``session_id`` is already in ``active`` or ``paused``.
        """
        if (user_id is None) == (service_account_id is None):
            raise ValueError(
                "MissionStore.create requires exactly one of user_id / "
                "service_account_id (the principal that owns the mission)"
            )
        async with self._sf() as db:
            existing = await db.scalar(
                select(MissionRow.id)
                .where(
                    MissionRow.session_id == session_id,
                    MissionRow.status.in_(_ACTIVE_OR_PAUSED),
                )
                .limit(1)
            )
            if existing is not None:
                raise ActiveMissionConflictError(
                    f"session {session_id} already has an active or paused mission"
                )
            row = MissionRow(
                org_id=org_id,
                user_id=user_id,
                service_account_id=service_account_id,
                session_id=session_id,
                agent_id=agent_id,
                description=description,
                rubric=rubric,
                max_iterations=max_iterations,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return row.id

    async def get(self, mission_id: UUID) -> Mission:
        async with self._sf() as db:
            row = await db.get(MissionRow, mission_id)
            if row is None:
                raise MissionNotFoundError(f"mission {mission_id} not found")
            return Mission.model_validate(row)

    async def get_active_for_session(self, session_id: UUID) -> Mission | None:
        """Return the session's active or paused mission, if any."""
        async with self._sf() as db:
            row = await db.scalar(
                select(MissionRow)
                .where(
                    MissionRow.session_id == session_id,
                    MissionRow.status.in_(_ACTIVE_OR_PAUSED),
                )
                .limit(1)
            )
        if row is None:
            return None
        return Mission.model_validate(row)

    async def set_status(
        self,
        mission_id: UUID,
        status: MissionStatus,
        *,
        paused_reason: str | None = None,
        cancelled_reason: str | None = None,
    ) -> None:
        values: dict[str, Any] = {"status": status}
        if paused_reason is not None:
            values["paused_reason"] = paused_reason
        if cancelled_reason is not None:
            values["cancelled_reason"] = cancelled_reason
        async with self._sf() as db:
            result = await db.execute(
                update(MissionRow)
                .where(MissionRow.id == mission_id)
                .values(**values)
            )
            if result.rowcount == 0:
                raise MissionNotFoundError(f"mission {mission_id} not found")
            await db.commit()

    async def record_evaluation(
        self,
        mission_id: UUID,
        *,
        result: str,
        explanation: str,
        feedback: str,
    ) -> None:
        async with self._sf() as db:
            res = await db.execute(
                update(MissionRow)
                .where(MissionRow.id == mission_id)
                .values(
                    last_evaluation_result=result,
                    last_evaluation_explanation=explanation,
                    last_evaluation_feedback=feedback,
                    last_evaluation_at=func.now(),
                    evaluator_parse_failures=0,
                )
            )
            if res.rowcount == 0:
                raise MissionNotFoundError(f"mission {mission_id} not found")
            await db.commit()

    async def increment_iteration(self, mission_id: UUID) -> int:
        """Bump iteration by 1; return the new value."""
        async with self._sf() as db:
            res = await db.execute(
                update(MissionRow)
                .where(MissionRow.id == mission_id)
                .values(iteration=MissionRow.iteration + 1)
                .returning(MissionRow.iteration)
            )
            new_iter = res.scalar_one_or_none()
            if new_iter is None:
                raise MissionNotFoundError(f"mission {mission_id} not found")
            await db.commit()
            return int(new_iter)

    async def record_parse_failure(self, mission_id: UUID) -> int:
        """Increment parse failures and pause the mission after 3 in a row."""
        async with self._sf() as db:
            res = await db.execute(
                update(MissionRow)
                .where(MissionRow.id == mission_id)
                .values(
                    evaluator_parse_failures=MissionRow.evaluator_parse_failures + 1,
                    paused_reason=case(
                        (
                            MissionRow.evaluator_parse_failures + 1 >= 3,
                            "evaluator parse failure",
                        ),
                        else_=MissionRow.paused_reason,
                    ),
                    status=case(
                        (
                            MissionRow.evaluator_parse_failures + 1 >= 3,
                            "paused",
                        ),
                        else_=MissionRow.status,
                    ),
                )
                .returning(MissionRow.evaluator_parse_failures)
            )
            failures = res.scalar_one_or_none()
            if failures is None:
                raise MissionNotFoundError(f"mission {mission_id} not found")
            await db.commit()
            return int(failures)

    async def recently_evaluated(
        self, mission_id: UUID, *, window_seconds: int,
    ) -> bool:
        """Return True iff ``last_evaluation_at`` is within ``window_seconds``."""
        async with self._sf() as db:
            row = await db.get(MissionRow, mission_id)
            if row is None:
                raise MissionNotFoundError(f"mission {mission_id} not found")
            if row.last_evaluation_at is None:
                return False
            last = row.last_evaluation_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) - last < timedelta(seconds=window_seconds)
