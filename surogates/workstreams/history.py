"""A project's history on the server: its lock, and the landing sagas' rows.

The lock is a Postgres advisory transaction lock keyed by
``workstream:<id>``.  A landing holds it for its whole saga; it frees itself
when its connection drops.  A row of ``workstream_history`` is a landing
saga's durable record, written as the saga runs, so the next holder of the
lock can finish or undo a landing whose worker died.  Its steps are written
whole, so every few seconds rather than at each: between those writes a
try of a step only marks the row alive.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from itertools import islice
from typing import Any
from uuid import UUID

from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import Range

from surogates.db.models import WorkstreamHistory, WorkstreamThread
from surogates.governance.saga import Saga
from surogates.sandbox.history import HISTORY_CAP, PRUNE_DAYS, tracked
from surogates.storage.tenant import boundary_workspace_prefix

logger = logging.getLogger(__name__)

#: How long a landing waits between tries for its project's lock.
LOCK_POLL = 0.5
#: How long a project's count against the cap is taken again, and each one's
#: answer with when it was counted.
COUNT_TTL = 60
# Per worker, an entry a project, never pruned: the projects a worker serves are few.
_COUNTED: dict[tuple[str, str], tuple[float, bool]] = {}


@asynccontextmanager
async def project_lock(session_factory: Any, workstream_id: UUID | str) -> AsyncIterator[Callable[[], Awaitable[None]]]:
    """Hold *workstream_id*'s lock for the block, on a connection of its own; a check that it is still held.

    Tried, not waited for in Postgres: a landing waiting for the lock holds
    no pooled connection between its tries.  The lock goes with its
    connection, unseen, so the check asks that connection to answer.
    """
    key = func.hashtext(f"workstream:{workstream_id}")
    while True:
        async with session_factory() as db, db.begin():
            if (await db.execute(select(func.pg_try_advisory_xact_lock(key)))).scalar():

                async def held() -> None:
                    if not db.in_transaction():
                        # The block has ended, and the lock with it.
                        raise RuntimeError("the project's lock was let go")
                    await db.execute(select(1))

                yield held
                return
        await asyncio.sleep(LOCK_POLL)


async def start_landing(
    session_factory: Any, saga: Saga, *, workstream_id: UUID | str, thread_id: UUID, agent_id: str,
    user_id: UUID | None, tool_saga_id: str | None, events: tuple[int, int] | None,
) -> int:
    """The row of a landing about to take its first step, the saga ``running``; its id."""
    async with session_factory() as db, db.begin():
        return (await db.execute(
            insert(WorkstreamHistory).values(
                workstream_id=workstream_id, kind="landing", saga_id=saga.saga_id, saga_state="running",
                steps=saga.to_dict()["steps"], thread_id=thread_id, tool_saga_id=tool_saga_id,
                events=Range(events[0], events[1], bounds="[]") if events else None,
                agent_id=agent_id, user_id=user_id,
            ).returning(WorkstreamHistory.id)
        )).scalar_one()


async def save_landing(
    session_factory: Any, row: int, saga: Saga, *, state: str = "running",
    commit: str | None = None, files: list[dict] | None = None,
) -> None:
    """Write the saga's steps as they are into its row, and its outcome once it has one."""
    values: dict[str, Any] = {"steps": saga.to_dict()["steps"], "saga_state": state}
    if commit is not None:
        values["commit"] = commit
    if files is not None:
        values["files"] = files
    async with session_factory() as db, db.begin():
        await db.execute(update(WorkstreamHistory).where(WorkstreamHistory.id == row).values(**values))


async def touch_landing(session_factory: Any, row: int) -> None:
    """Mark the row alive, its steps as they were: a try of a step is starting."""
    async with session_factory() as db, db.begin():
        await db.execute(update(WorkstreamHistory).where(WorkstreamHistory.id == row).values(updated_at=func.now()))


async def running_landings(session_factory: Any, workstream_id: UUID | str) -> list[tuple[WorkstreamHistory, float]]:
    """The project's landings still ``running``, oldest first, each with the seconds since its row last changed."""
    quiet = func.extract("epoch", func.now() - WorkstreamHistory.updated_at)
    async with session_factory() as db:
        rows = await db.execute(
            select(WorkstreamHistory, quiet)
            .where(WorkstreamHistory.workstream_id == workstream_id, WorkstreamHistory.saga_state == "running")
            .order_by(WorkstreamHistory.id)
        )
        return [(row, float(seconds)) for row, seconds in rows.all()]


def saga_of(row: WorkstreamHistory) -> Saga:
    """The landing saga *row* records, rebuilt as it stood.

    A put-back the kill cut off is run again, and so is one that failed
    before the kill wrote its landing ``escalated``: putting a file back is
    safe to repeat.  A thread deleted since leaves no ``thread_id``, and the
    saga's session is the nil id.
    """
    again = ("compensating", "compensation_failed")
    steps = [{**s, "state": "committed"} if s["state"] in again else s for s in row.steps]
    return Saga.from_dict({
        "saga_id": row.saga_id, "session_id": str(row.thread_id or UUID(int=0)), "kind": "landing", "state": "running",
        "created_at": row.created_at.isoformat(), "completed_at": None, "error": None, "steps": steps,
    })


async def waits_to_land(session_factory: Any, storage: Any, session: Any) -> bool:
    """Whether a thread's turn that never used its pod lands at its end all the same.

    It does while its branch in the project's history holds work its base
    lacks, such as a failed turn's, while its helpers kept work on its
    hand-off it has not taken up, or while a landing of the project is left
    running for a lock holder to settle.
    """
    if await running_landings(session_factory, session.config["workstream_id"]):
        return True
    prefix = boundary_workspace_prefix(session.config, session, session.id)
    try:
        text = (await storage.read(session.config["storage_bucket"], f"{prefix}_history/packed-refs")).decode()
    except KeyError:
        return False
    refs = {ref: sha for sha, _, ref in (line.partition(" ") for line in text.splitlines())}
    return any(refs.get(a) != refs.get(b) for a, b in (
        (f"refs/heads/threads/{session.id}", f"refs/bases/{session.id}"),
        (f"refs/handoff/{session.id}", f"refs/handoff-from/{session.id}"),
    ))


async def over_history_cap(storage: Any, session: Any) -> bool:
    """Whether the project of a thread, or a thread's helper, has more files than history keeps.

    Counted from the bucket's listing, before a pod is made: a pod's layout
    is fixed when it is.  Off the event loop, and stopped one past the cap;
    a wake within a minute of the last count takes its answer.  A listing
    that fails fails no wake: the last count stands, and with none history
    stays on, until the next wake counts.
    """
    bucket = session.config["storage_bucket"]
    prefix = boundary_workspace_prefix(session.config, session, session.id)
    counted = _COUNTED.get((bucket, prefix))
    if counted is not None and time.monotonic() - counted[0] < COUNT_TTL:
        return counted[1]
    try:
        keys = await storage.list_keys(bucket, prefix)
    except Exception:
        logger.warning("Could not count the files of %s/%s against the history's cap", bucket, prefix, exc_info=True)
        return counted[1] if counted is not None else False

    def over() -> bool:
        # A key ending in / is a folder's marker, which geesefs writes.
        files = (k for k in keys if not k.endswith("/") and tracked(k.removeprefix(prefix)))
        return next(islice(files, HISTORY_CAP, None), None) is not None

    answer = await asyncio.to_thread(over)
    _COUNTED[(bucket, prefix)] = (time.monotonic(), answer)
    return answer


async def kept_refs(session_factory: Any, workstream_id: UUID | str) -> list[str]:
    """The refs a pruning keeps: each live thread's, and each resolved within the window.

    A thread's branch and base, its hand-off, and its helpers' copies kept apart.
    """
    ended = WorkstreamThread.resolved_at > func.now() - timedelta(days=PRUNE_DAYS)
    async with session_factory() as db:
        threads = await db.execute(
            select(WorkstreamThread.session_id)
            .where(WorkstreamThread.workstream_id == workstream_id, or_(WorkstreamThread.resolved_at.is_(None), ended))
        )
        return [
            ref for thread in threads.scalars()
            for ref in (
                f"refs/heads/threads/{thread}", f"refs/bases/{thread}",
                f"refs/handoff/{thread}", f"refs/handoff-from/{thread}", f"refs/helpers/{thread}/",
            )
        ]
