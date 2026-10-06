"""The ``workstreams`` rows: one user's projects for one agent."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from surogates.db.models import Workstream


def _owned(workstream_id: UUID | None, *, org_id: UUID, agent_id: str, user_id: UUID) -> tuple[Any, ...]:
    """The owner's live projects, or the one with *workstream_id* among them."""
    clauses = (
        Workstream.org_id == org_id,
        Workstream.agent_id == agent_id,
        Workstream.user_id == user_id,
        Workstream.status == "active",
    )
    return clauses if workstream_id is None else (*clauses, Workstream.id == workstream_id)


class WorkstreamStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def create(self, **values: Any) -> Workstream:
        row = Workstream(**values)
        async with self._sf() as db:
            db.add(row)
            await db.commit()
            await db.refresh(row)
        return row

    async def list(self, **owner: Any) -> list[Workstream]:
        async with self._sf() as db:
            rows = await db.scalars(
                select(Workstream).where(*_owned(None, **owner)).order_by(Workstream.updated_at.desc())
            )
            return list(rows)

    async def get(self, workstream_id: UUID, **owner: Any) -> Workstream | None:
        async with self._sf() as db:
            return await db.scalar(select(Workstream).where(*_owned(workstream_id, **owner)))
