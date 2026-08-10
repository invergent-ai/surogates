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

from surogates.db.models import (
    Mission as MissionRow,
    Session as ORMSession,
    Task,
)
from surogates.missions.models import Mission, MissionStatus


_TERMINAL_STATUSES: tuple[str, ...] = (
    "satisfied", "blocked", "failed", "cancelled", "max_iterations_reached",
)
_ACTIVE_OR_PAUSED: tuple[str, ...] = ("active", "paused")


STAGNANT_EVALUATION_LIMIT = 3


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
        budget_tokens: int | None = None,
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
            budget_tokens=budget_tokens,
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
        elif status == "active":
            # A running mission has no reason to be paused. Leaving the old
            # one behind is not merely cosmetic: `/mission accept|reject`
            # authorize on ``paused_reason == "awaiting_refinement"``, so a
            # stale value would let a resumed, working mission be terminated
            # or have its rubric swapped out from under it.
            values["paused_reason"] = None
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
                    # A verdict that is not "satisfied" means the loop did
                    # not move; anything else starts the count over.
                    stagnant_evaluations=(
                        0 if result == "satisfied"
                        else MissionRow.stagnant_evaluations + 1
                    ),
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

    async def set_budget(
        self, mission_id: UUID, *, budget_tokens: int | None,
    ) -> None:
        """Set or clear a mission's token allowance.

        ``None`` removes the ceiling. Settable after creation because that is
        when the need usually shows up — a mission already running turns out
        to be burning more than expected.
        """
        async with self._sf() as db:
            res = await db.execute(
                update(MissionRow)
                .where(MissionRow.id == mission_id)
                .values(budget_tokens=budget_tokens, updated_at=func.now())
            )
            if res.rowcount == 0:
                raise MissionNotFoundError(f"mission {mission_id} not found")
            await db.commit()

    async def amend_rubric(self, mission_id: UUID, *, new_rubric: str) -> None:
        """Replace the rubric and put the mission back to work.

        The caller passes text it read from a ``mission.refinement_proposed``
        event, never text from the coordinator or from the command line.

        ``iteration`` is deliberately untouched: the amendment changes the
        target, not the allowance. A mission that burned 18 of 20 iterations
        getting the target wrong does not get 20 more for free --
        ``/mission budget`` funds a pivot explicitly.

        ``description`` is never written. It is the standing intent.
        """
        cleaned = (new_rubric or "").strip()
        if not cleaned:
            raise ValueError("amend_rubric requires a non-empty rubric")
        async with self._sf() as db:
            res = await db.execute(
                update(MissionRow)
                .where(MissionRow.id == mission_id)
                .values(
                    rubric=cleaned,
                    status="active",
                    paused_reason=None,
                    stagnant_evaluations=0,
                    updated_at=func.now(),
                )
            )
            if res.rowcount == 0:
                raise MissionNotFoundError(f"mission {mission_id} not found")
            await db.commit()

    async def is_stagnant(self, mission_id: UUID) -> bool:
        """True when the evaluator has returned no progress too many times.

        3 is a surogates choice, not a port: it matches the parse-failure
        pause and the recovery ceiling, so an operator meets one number.
        """
        async with self._sf() as db:
            count = (await db.execute(
                select(MissionRow.stagnant_evaluations)
                .where(MissionRow.id == mission_id)
            )).scalar_one_or_none()
        return bool(count is not None and count >= STAGNANT_EVALUATION_LIMIT)
    async def tokens_spent(self, mission_id: UUID) -> int:
        """Total tokens billed to *mission_id*.

        DERIVED, never a counter: a SUM over the mission's own coordinator
        session plus every session attached to one of its tasks.  A counter
        incremented alongside the work can drift from what was actually
        billed; this cannot.

        The join goes ``sessions.task_id -> tasks.mission_id`` rather than
        reading ``Task.current_session_id``, which only points at the
        in-flight attempt and would miss every earlier one.
        """
        spend = func.coalesce(ORMSession.input_tokens, 0) + func.coalesce(
            ORMSession.output_tokens, 0
        )
        async with self._sf() as db:
            own = (await db.execute(
                select(func.coalesce(func.sum(spend), 0))
                .select_from(ORMSession)
                .join(MissionRow, MissionRow.session_id == ORMSession.id)
                .where(MissionRow.id == mission_id)
            )).scalar_one()
            workers = (await db.execute(
                select(func.coalesce(func.sum(spend), 0))
                .select_from(ORMSession)
                .join(Task, Task.id == ORMSession.task_id)
                .where(Task.mission_id == mission_id)
            )).scalar_one()
        return int(own or 0) + int(workers or 0)

    async def budget_exhausted(self, mission_id: UUID) -> bool:
        """True when the mission has a token allowance and has spent it."""
        async with self._sf() as db:
            budget = (await db.execute(
                select(MissionRow.budget_tokens)
                .where(MissionRow.id == mission_id)
            )).scalar_one_or_none()
        if not budget:
            return False
        return await self.tokens_spent(mission_id) >= budget

    async def pause_if_budget_exhausted(self, mission_id: UUID) -> bool:
        """Pause a mission that has spent its allowance. True if this call did it.

        Reuses ``paused`` + ``paused_reason`` rather than adding a status:
        a new one would have to land in the SDK's ``types.ts`` and its
        rebuilt dist, which is a known release-breaker, and "paused with a
        reason" is exactly what this is.
        """
        if not await self.budget_exhausted(mission_id):
            return False
        async with self._sf() as db:
            updated = (await db.execute(
                update(MissionRow)
                .where(
                    MissionRow.id == mission_id,
                    MissionRow.status == "active",
                )
                .values(
                    status="paused",
                    paused_reason="budget_exhausted",
                    updated_at=func.now(),
                )
                .returning(MissionRow.id)
            )).scalar_one_or_none()
            await db.commit()
        return updated is not None
