"""The ``workstreams`` rows: one user's projects for one agent."""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from surogates.db.models import Event, InboxItem, Workstream, WorkstreamThread
from surogates.db.models import Session as SessionRow
from surogates.session.events import EventType
from surogates.workstreams import master_instructions
from surogates.workstreams.derive import LATEST_TYPES, WAITING_KINDS, ThreadFacts


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

    async def change(self, workstream_id: UUID, changes: dict[str, Any], **owner: Any) -> Workstream | None:
        """Apply *changes* to a live project of the owner's and write them
        through to its master, in one transaction; None when there is none.

        The project row's lock orders two changes sent at once, so the master
        ends with the instructions and tier of the one that lands last.
        """
        async with self._sf() as db:
            row = await db.scalar(
                update(Workstream).where(*_owned(workstream_id, **owner)).values(**changes).returning(Workstream)
            )
            if row is None:
                return None
            master = await db.get(SessionRow, row.master_session_id, with_for_update=True)
            config = dict(master.config or {})
            config["system"] = master_instructions(row.name, row.goal, row.instructions)
            if row.coordinator_tier is None:
                config.pop("workstream_tier", None)
            else:
                config["workstream_tier"] = row.coordinator_tier
            master.config = config
            master.title = row.name
            await db.commit()
            return row

    async def add_thread(self, session_id: UUID, workstream_id: UUID, title: str) -> None:
        """Record *session_id* as a thread of the project, and title its chat."""
        async with self._sf() as db:
            db.add(WorkstreamThread(session_id=session_id, workstream_id=workstream_id, title=title))
            await db.execute(update(SessionRow).where(SessionRow.id == session_id).values(title=title))
            await db.commit()

    async def get_thread(self, session_id: UUID) -> WorkstreamThread | None:
        async with self._sf() as db:
            return await db.get(WorkstreamThread, session_id)

    async def reopen_thread(self, session_id: UUID) -> None:
        """New work for a thread takes it out of Resolved."""
        async with self._sf() as db:
            await db.execute(
                update(WorkstreamThread).where(WorkstreamThread.session_id == session_id).values(resolved_at=None)
            )
            await db.commit()

    async def thread_facts(self, workstream_id: UUID, *, thread_id: UUID | None = None) -> list[ThreadFacts]:
        """What the rows of the project's threads are derived from, or only
        *thread_id*'s.  A deleted thread is left out."""
        query = (
            select(WorkstreamThread, SessionRow.status, SessionRow.updated_at)
            .join(SessionRow, SessionRow.id == WorkstreamThread.session_id)
            .where(WorkstreamThread.workstream_id == workstream_id, SessionRow.status != "archived")
        )
        if thread_id is not None:
            query = query.where(WorkstreamThread.session_id == thread_id)
        async with self._sf() as db:
            threads = (await db.execute(query)).all()
            ids = [thread.session_id for thread, _, _ in threads]
            if not ids:
                return []
            # A thread's delegated children wait on the user for it, so every
            # session under a thread counts as the thread's.
            tree = select(SessionRow.id, SessionRow.id.label("thread_id")).where(SessionRow.id.in_(ids))
            tree = tree.cte("tree", recursive=True)
            tree = tree.union_all(
                select(SessionRow.id, tree.c.thread_id).join(tree, SessionRow.parent_id == tree.c.id)
            )
            thread_of = dict((await db.execute(select(tree.c.id, tree.c.thread_id))).all())
            # Pending items, and the expired questions the rules read.
            items = await db.scalars(select(InboxItem).where(
                InboxItem.session_id.in_(thread_of),
                InboxItem.kind.in_(WAITING_KINDS),
                or_(
                    InboxItem.status == "pending",
                    and_(InboxItem.status == "expired", InboxItem.kind == "input_required"),
                ),
            ))
            # The newest event of each type the rules read, per thread: the
            # ids first, aggregated from narrow rows, then those rows.  And
            # every turn summary, for the files.
            newest = (
                select(func.max(Event.id))
                .where(Event.session_id.in_(ids), Event.type.in_(LATEST_TYPES))
                .group_by(Event.session_id, Event.type)
            )
            latest = await db.scalars(select(Event).where(Event.id.in_(newest)))
            summaries = await db.scalars(select(Event).where(
                Event.session_id.in_(ids), Event.type == EventType.TURN_SUMMARY.value,
            ))
            items_of, events_of = defaultdict(list), defaultdict(list)
            for item in items:
                items_of[thread_of[item.session_id]].append(item)
            for event in (*latest, *summaries):
                events_of[event.session_id].append(event)
        return [
            ThreadFacts(
                id=thread.session_id, title=thread.title, status=status,
                created_at=thread.created_at, updated_at=updated_at, resolved_at=thread.resolved_at,
                # Every thread works in the cloud until local-folder threads.
                place={"kind": "cloud"},
                items=tuple(items_of[thread.session_id]), events=tuple(events_of[thread.session_id]),
            )
            for thread, status, updated_at in sorted(threads, key=lambda found: found[2], reverse=True)
        ]

    async def latest_report(self, master_id: UUID, thread_id: UUID) -> Event | None:
        """The last report *thread_id* sent its master, as the master read it."""
        async with self._sf() as db:
            return await db.scalar(
                select(Event)
                .where(
                    Event.session_id == master_id,
                    Event.type.in_((EventType.WORKER_COMPLETE.value, EventType.WORKER_FAILED.value)),
                    Event.data["worker_id"].astext == str(thread_id),
                )
                .order_by(Event.id.desc())
                .limit(1)
            )
