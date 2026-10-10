"""The ``workstreams`` rows: one user's projects for one agent."""

from __future__ import annotations

import posixpath
from collections import defaultdict
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, any_, bindparam, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from surogates.db.models import Event, InboxItem, Workstream, WorkstreamHistory, WorkstreamThread
from surogates.db.models import Session as SessionRow
from surogates.session.events import MESSAGE_TYPES, EventType
from surogates.workstreams import master_instructions
from surogates.workstreams.derive import LATEST_TYPES, WAITING_KINDS, ThreadFacts, place_of, undone_files


def _any(ids: Any) -> Any:
    """*ids* bound as one array, however many: asyncpg takes at most 32,767
    parameters a statement, and an ``IN`` list binds one per id."""
    return any_(bindparam(None, list(ids), type_=ARRAY(PG_UUID(as_uuid=True))))


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

    async def add_thread(self, session_id: UUID, workstream_id: UUID, title: str) -> bool:
        """Record *session_id* as a thread of the project, and title its chat;
        False, writing nothing, when the project has been archived.

        The project's row is held while the thread is added, and an archive
        takes it before it reads the tree it archives, so a thread is either
        in that tree or refused here.
        """
        async with self._sf() as db:
            live = await db.scalar(
                select(Workstream.id)
                .where(Workstream.id == workstream_id, Workstream.status == "active")
                .with_for_update(read=True)
            )
            if live is None:
                return False
            db.add(WorkstreamThread(session_id=session_id, workstream_id=workstream_id, title=title))
            await db.execute(update(SessionRow).where(SessionRow.id == session_id).values(title=title))
            await db.commit()
            return True

    async def get_thread(self, session_id: UUID) -> WorkstreamThread | None:
        async with self._sf() as db:
            return await db.get(WorkstreamThread, session_id)

    async def reopen_thread(self, session_id: UUID) -> None:
        """Take a thread out of Resolved, its own or seven quiet days': new
        work for it, and the user's reopen, are its activity."""
        async with self._sf() as db:
            await db.execute(
                update(WorkstreamThread).where(WorkstreamThread.session_id == session_id).values(resolved_at=None)
            )
            await db.execute(update(SessionRow).where(SessionRow.id == session_id).values(updated_at=func.now()))
            await db.commit()

    async def pause_thread(self, session_id: UUID) -> bool:
        """Pause *session_id* if it is working; whether it was.

        Conditional, so a stop that lands as the thread's turn ends leaves
        the thread as its turn left it.
        """
        async with self._sf() as db:
            result = await db.execute(
                update(SessionRow).where(SessionRow.id == session_id, SessionRow.status == "active")
                .values(status="paused", updated_at=func.now())
            )
            await db.commit()
            return result.rowcount == 1

    async def resolve_thread(self, session_id: UUID) -> None:
        """Move the thread to Resolved; one already there keeps the moment it got there."""
        async with self._sf() as db:
            await db.execute(
                update(WorkstreamThread)
                .where(WorkstreamThread.session_id == session_id, WorkstreamThread.resolved_at.is_(None))
                .values(resolved_at=func.now())
            )
            await db.commit()

    async def thread_facts(
        self, workstream_id: UUID, *, thread_id: UUID | None = None, with_files: bool = True,
    ) -> list[ThreadFacts]:
        """What the rows of the project's threads are derived from, or only
        *thread_id*'s.  A deleted thread is left out.

        Without files, only each thread's newest turn summary is read, for
        its status line, and a row's files are that turn's alone: reading
        every summary is most of the cost of a project's rows.  Nor are its
        landings read then, which mark its files, nor the project's Undos.
        """
        query = (
            select(WorkstreamThread, SessionRow.status, SessionRow.updated_at, SessionRow.config["execution"])
            .join(SessionRow, SessionRow.id == WorkstreamThread.session_id)
            .where(WorkstreamThread.workstream_id == workstream_id, SessionRow.status != "archived")
        )
        if thread_id is not None:
            query = query.where(WorkstreamThread.session_id == thread_id)
        async with self._sf() as db:
            threads = (await db.execute(query)).all()
            ids = [thread.session_id for thread, _, _, _ in threads]
            if not ids:
                return []
            # A thread's delegated children wait on the user for it, so every
            # session under a thread counts as the thread's.
            tree = select(SessionRow.id, SessionRow.id.label("thread_id")).where(SessionRow.id == _any(ids))
            tree = tree.cte("tree", recursive=True)
            tree = tree.union_all(
                select(SessionRow.id, tree.c.thread_id).join(tree, SessionRow.parent_id == tree.c.id)
            )
            thread_of = dict((await db.execute(select(tree.c.id, tree.c.thread_id))).all())
            # Pending items, and the expired questions the rules read.
            items = await db.scalars(select(InboxItem).where(
                InboxItem.session_id == _any(thread_of),
                InboxItem.kind.in_(WAITING_KINDS),
                or_(
                    InboxItem.status == "pending",
                    and_(InboxItem.status == "expired", InboxItem.kind == "input_required"),
                ),
            ))
            # The newest event of each type the rules read, per thread: the
            # ids first, aggregated from narrow rows, then those rows.  And
            # every turn summary, for the files, or only the newest.
            types = LATEST_TYPES if with_files else (*LATEST_TYPES, EventType.TURN_SUMMARY.value)
            newest = (
                select(func.max(Event.id))
                .where(Event.session_id == _any(ids), Event.type.in_(types))
                .group_by(Event.session_id, Event.type)
            )
            latest = await db.scalars(select(Event).where(Event.id.in_(newest)))
            summaries = await db.scalars(select(Event).where(
                Event.session_id == _any(ids), Event.type == EventType.TURN_SUMMARY.value,
            )) if with_files else ()
            # The files each landing recorded, and the project's Undos, which mark a
            # row's: the cloud's records, asked by project, which the table's index is on.
            here = (WorkstreamHistory.workstream_id == workstream_id, WorkstreamHistory.device_id.is_(None))
            landings = (await db.execute(
                select(WorkstreamHistory.thread_id, WorkstreamHistory.id, WorkstreamHistory.files).where(
                    *here, WorkstreamHistory.thread_id == _any(ids), WorkstreamHistory.kind == "landing",
                    WorkstreamHistory.saga_state == "completed", func.jsonb_array_length(WorkstreamHistory.files) > 0,
                )
            )).all() if with_files else ()
            undos = (await db.execute(
                select(WorkstreamHistory.id, WorkstreamHistory.undoes, WorkstreamHistory.files).where(
                    *here, WorkstreamHistory.kind == "undo", WorkstreamHistory.saga_state == "completed",
                )
            )).all() if with_files else ()
            items_of, events_of, landings_of = defaultdict(list), defaultdict(list), defaultdict(list)
            for item in items:
                items_of[thread_of[item.session_id]].append(item)
            for event in (*latest, *summaries):
                events_of[event.session_id].append(event)
            for landed_by, row_id, files in landings:
                landings_of[landed_by].append({"id": row_id, "files": files})
        undone = undone_files({"id": row_id, "undoes": undoes, "files": files} for row_id, undoes, files in undos)
        redoing = await self._redoing(landings_of)
        return [
            ThreadFacts(
                id=thread.session_id, title=thread.title, status=status,
                created_at=thread.created_at, updated_at=updated_at, resolved_at=thread.resolved_at,
                place=place_of(execution),
                items=tuple(items_of[thread.session_id]), events=tuple(events_of[thread.session_id]),
                landings=tuple(landings_of[thread.session_id]), redoing=redoing.get(thread.session_id, frozenset()),
                undone=undone,
            )
            for thread, status, updated_at, execution in sorted(threads, key=lambda found: found[2], reverse=True)
        ]

    async def _redoing(self, landings_of: dict[UUID, list[dict[str, Any]]]) -> dict[UUID, frozenset[str]]:
        """The files each thread's next turn is told to redo, by the landing's
        own rule (``landing.redo_files``), so that a row says a file is being
        redone exactly while that turn would redo it.  Asked only of a thread
        with a file a landing left out: no other has one to mark.
        """
        from surogates.harness.landing import redo_files
        from surogates.session.store import SessionStore

        sessions = SessionStore(self._sf)
        return {
            thread_id: frozenset(await redo_files(sessions, thread_id))
            for thread_id, landings in landings_of.items()
            if any(not f["merged"] for landing in landings for f in landing["files"])
        }

    async def thread_counts(self, workstream_ids: list[UUID]) -> dict[UUID, tuple[datetime, int, int]]:
        """Each project's latest thread activity, and its threads waiting on
        the user and working, as ``derive_thread`` groups them; a project with
        no live thread is left out.

        Counted in SQL over the threads not resolved, the only ones that can
        count, so a list costs the user's open work, not every thread they
        ever started.  The quiet rule only moves an idle thread, which counts
        for nothing, so it is not read.
        """
        live = (
            select(
                WorkstreamThread.workstream_id, WorkstreamThread.session_id.label("id"),
                WorkstreamThread.resolved_at, SessionRow.status, SessionRow.updated_at,
            )
            .join(SessionRow, SessionRow.id == WorkstreamThread.session_id)
            .where(WorkstreamThread.workstream_id == _any(workstream_ids), SessionRow.status != "archived")
            .cte("live")
        )
        # As ``thread_facts`` reads them: every session under a thread is the thread's.
        tree = select(live.c.id, live.c.id.label("thread_id")).where(live.c.resolved_at.is_(None))
        tree = tree.cte("tree", recursive=True)
        tree = tree.union_all(select(SessionRow.id, tree.c.thread_id).join(tree, SessionRow.parent_id == tree.c.id))
        replied = (
            select(func.coalesce(func.max(Event.id), 0))
            .where(Event.session_id == tree.c.thread_id, Event.type.in_([t.value for t in MESSAGE_TYPES]))
            .scalar_subquery()
        )
        # A pending item, or a question that expired with no message after it.
        asking = (
            select(tree.c.thread_id)
            .join(InboxItem, InboxItem.session_id == tree.c.id)
            .where(
                InboxItem.kind.in_(WAITING_KINDS),
                or_(
                    InboxItem.status == "pending",
                    and_(
                        InboxItem.status == "expired", InboxItem.kind == "input_required",
                        InboxItem.source_event_id > replied,
                    ),
                ),
            )
            .distinct()
            .cte("asking")
        )
        unresolved, asks = live.c.resolved_at.is_(None), asking.c.thread_id.is_not(None)
        query = (
            select(
                live.c.workstream_id,
                func.max(live.c.updated_at),
                func.count().filter(unresolved & (asks | (live.c.status == "failed"))),
                func.count().filter(unresolved & ~asks & (live.c.status == "active")),
            )
            .select_from(live.outerjoin(asking, asking.c.thread_id == live.c.id))
            .group_by(live.c.workstream_id)
        )
        async with self._sf() as db:
            # The walk's row estimate is far above what it finds, which would
            # have the planner compile a read of a few milliseconds for longer
            # than it runs.
            await db.execute(text("SET LOCAL jit = off"))
            found = await db.execute(query)
            return {project: (latest, waiting, working) for project, latest, waiting, working in found}

    async def produced(self, workstream_id: UUID) -> list[tuple[UUID, str, Any, datetime]]:
        """Each file the project's threads' turn summaries named, the oldest
        naming first: the thread, the file's path from the top of where the
        thread works, where it works (its ``config.execution``: None in the
        cloud) and when the summary was written.  A ref outside where the
        thread works names none of its files."""
        async with self._sf() as db:
            summaries = await db.execute(
                select(
                    Event.session_id, Event.data["artifacts"], Event.created_at,
                    SessionRow.config["execution"], SessionRow.config["workspace_path"],
                )
                .join(WorkstreamThread, WorkstreamThread.session_id == Event.session_id)
                .join(SessionRow, SessionRow.id == Event.session_id)
                .where(WorkstreamThread.workstream_id == workstream_id, Event.type == EventType.TURN_SUMMARY.value)
                .order_by(Event.id)
            )
            produced: list[tuple[UUID, str, Any, datetime]] = []
            for thread_id, artifacts, at, execution, folder in summaries:
                # The model's argument as given: ``./a.docx`` and the whole
                # path, ``/workspace/a.docx`` or the computer's folder's, are ``a.docx``.
                top = f"{str(folder).rstrip('/')}/" if execution else "/workspace/"
                for artifact in artifacts if isinstance(artifacts, list) else []:
                    if isinstance(artifact, dict) and artifact.get("kind") == "file" and isinstance(artifact.get("ref"), str):
                        path = posixpath.normpath(artifact["ref"]).removeprefix(top)
                        if not path.startswith("/") and path.split("/")[0] != "..":
                            produced.append((thread_id, path, execution, at))
        return produced

    async def masters(self, master_ids: list[UUID]) -> dict[UUID, tuple[datetime, bool]]:
        """Each master's last activity, and whether it waits on the user: a
        question or an approval pending in the project's conversation."""
        asking = (
            select(InboxItem.id)
            .where(
                InboxItem.session_id == SessionRow.id,
                InboxItem.kind.in_(WAITING_KINDS),
                InboxItem.status == "pending",
            )
            .exists()
        )
        async with self._sf() as db:
            found = await db.execute(
                select(SessionRow.id, SessionRow.updated_at, asking).where(SessionRow.id == _any(master_ids))
            )
            return {master_id: (updated_at, asks) for master_id, updated_at, asks in found}

    async def proposal(self, master_id: UUID, proposal_id: UUID) -> dict[str, Any] | None:
        """The master's ``thread.proposed`` payload for *proposal_id*."""
        async with self._sf() as db:
            data = await db.scalar(select(Event.data).where(
                Event.session_id == master_id,
                Event.type == EventType.THREAD_PROPOSED.value,
                Event.data["proposal_id"].astext == str(proposal_id),
            ))
        return data

    async def started_from(self, master_id: UUID, proposal_id: UUID, key: str) -> bool:
        """Whether a thread was started from the card *key* of *proposal_id*."""
        async with self._sf() as db:
            return await db.scalar(select(Event.id).where(
                Event.session_id == master_id,
                Event.type == EventType.WORKER_SPAWNED.value,
                Event.data["proposal_id"].astext == str(proposal_id),
                Event.data["key"].astext == key,
            ).limit(1)) is not None

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
