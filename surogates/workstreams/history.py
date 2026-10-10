"""A project's history on the server: its lock, and the landing sagas' rows.

The lock is a Postgres advisory transaction lock keyed by
``workstream:<id>``.  A landing holds it for its whole saga; it frees itself
when its connection drops.  A row of ``workstream_history`` is a landing
saga's durable record, written as the saga runs, so the next holder of the
lock can finish or undo a landing whose worker died.  Its steps are written
whole, so not at each: every five seconds, or every twenty times what a
write of them takes when that is longer.  Between those writes a try of a
step only marks the row alive.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from itertools import islice
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import Range

from surogates.db.models import WorkstreamHistory, WorkstreamThread
from surogates.governance.saga import Saga
from surogates.sandbox.history import HISTORY_CAP, PRUNE_DAYS, YOU, _who, tracked
from surogates.storage.tenant import boundary_workspace_prefix
from surogates.workstreams.derive import utc

logger = logging.getLogger(__name__)

#: How long a landing waits between tries for its project's lock.
LOCK_POLL = 0.5
#: How long a project's count against the cap is taken again, and each one's
#: answer with when it was counted.
COUNT_TTL = 60
#: The most of a history's ``packed-refs`` a turn's end reads into the worker.  A
#: thread's commands can write that file at any size; a project's own is a
#: few lines a thread.
REFS_BOUND = 4 * 2**20
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
    commit: str | None = None, files: list[dict] | None = None, picked_up: list[dict] | None = None,
) -> None:
    """Write the saga's steps as they are into its row, and its outcome once it has one."""
    values: dict[str, Any] = {"steps": saga.to_dict()["steps"], "saga_state": state}
    if commit is not None:
        values["commit"] = commit
    if files is not None:
        values["files"] = files
    if picked_up is not None:
        values["picked_up"] = picked_up
    async with session_factory() as db, db.begin():
        await db.execute(update(WorkstreamHistory).where(WorkstreamHistory.id == row).values(**values))


async def drop_landing(session_factory: Any, row: int) -> None:
    """Take away the row of a landing that changed no file: it is no change to the project's files."""
    async with session_factory() as db, db.begin():
        await db.execute(delete(WorkstreamHistory).where(WorkstreamHistory.id == row))


async def saved_through(session_factory: Any, thread_id: UUID) -> int | None:
    """The last tool call, by its event's id, of the thread's latest landing whose turn is in the history; None when it has none.

    A landing's commit step puts its turn on the thread's branch before
    any file lands.  The thread's next copy then has that work, though the
    worker died before the turn's end was written.  A landing that
    completed leaving a file out moved the branch on without the thread's
    version of it, so that one does not count.
    """
    async with session_factory() as db:
        row = (await db.execute(
            select(WorkstreamHistory)
            .where(WorkstreamHistory.thread_id == thread_id, WorkstreamHistory.kind == "landing")
            .order_by(WorkstreamHistory.id.desc()).limit(1)
        )).scalars().first()
    if row is None or row.events is None:
        return None
    commit = next((s for s in row.steps if s["tool_name"] == "history.commit" and s["state"] == "committed"), None)
    if commit is None or (row.saga_state == "completed" and commit["result"].get("overlapped")):
        return None
    return row.events.upper - (0 if row.events.upper_inc else 1)


async def record_pickup(
    session_factory: Any, saga: Saga, *, workstream_id: UUID | str, commit: str, picked_up: list[dict],
    agent_id: str, user_id: UUID | None,
) -> int:
    """The row of a pickup pushed alone, recorded: what the real files changed, a routine run's or yours before it; its id.

    Its one step's arguments name its author.
    """
    async with session_factory() as db, db.begin():
        return (await db.execute(
            insert(WorkstreamHistory).values(
                workstream_id=workstream_id, kind="pickup", saga_id=saga.saga_id, saga_state="completed",
                steps=saga.to_dict()["steps"], commit=commit, picked_up=picked_up, agent_id=agent_id, user_id=user_id,
            ).returning(WorkstreamHistory.id)
        )).scalar_one()


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


async def waits_to_land(session_factory: Any, storage: Any, session: Any, *, fence: float | None = None) -> bool:
    """Whether a thread's turn that never used its pod lands at its end all the same.

    It does while its branch in the project's history holds work its base
    lacks, such as a failed turn's, while its helpers kept work on its
    hand-off it has not taken up, or while a landing of the project is left
    running for a lock holder to settle.  With *fence*, only a landing
    whose row has been quiet for longer than it: one written since may be
    another thread's, alive, and a pod opened for it would only wait.
    """
    running = await running_landings(session_factory, session.config["workstream_id"])
    if any(fence is None or quiet >= fence for _, quiet in running):
        return True
    prefix = boundary_workspace_prefix(session.config, session, session.id)
    bucket, key = session.config["storage_bucket"], f"{prefix}_history/packed-refs"
    try:
        if (await storage.stat(bucket, key))["size"] > REFS_BOUND:
            # Not a size a history's refs have: left unread here.  The pod's open reads them, each line checked.
            return True
        text = (await storage.read(bucket, key)).decode(errors="replace")
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

    A thread's branch and base, its hand-off, and under its helpers' name
    their copies kept apart and the hand-offs whose versions its copy left
    out in a turn that changed nothing else.
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


#: What a project over the file cap answers wherever its history is asked for.
HISTORY_OFF = "History is off: this project has more than 50,000 files."
#: A row's id as a version names it: digits the column holds, never what a guess could overflow.
_ROW_ID = re.compile(r"[0-9]{1,18}")
#: The step whose author a version is by: a row's files are its commit's, and what it picked up its pickup's.
_STEP = {False: "history.commit", True: "history.pickup"}
# The project's cloud files that are gone: each one's newest landed record, where that took the file away, the
# newest first and :most of them, with who that record's step says it is by.  A row's files come after its pickup.
_GONE = text("""
    SELECT gone.id, gone.side, gone.path, h.updated_at, (
               SELECT s->'arguments'->'author' FROM jsonb_array_elements(h.steps) s
                WHERE s->>'tool_name' = CASE gone.side WHEN 'p' THEN :pickup ELSE :commit END LIMIT 1
           ) AS author
      FROM (
        SELECT id, side, path FROM (
            SELECT DISTINCT ON (path) id, side, path, after FROM (
                SELECT h.id, 'f' AS side, f->>'path' AS path, f->'after' AS after
                  FROM workstream_history h, jsonb_array_elements(h.files) f
                 WHERE h.workstream_id = :project AND h.device_id IS NULL AND h.saga_state = 'completed'
                   AND jsonb_typeof(f->'path') = 'string' AND f->'merged' IS DISTINCT FROM 'false'::jsonb
                UNION ALL
                SELECT h.id, 'p', p->>'path', p->'after'
                  FROM workstream_history h, jsonb_array_elements(h.picked_up) p
                 WHERE h.workstream_id = :project AND h.device_id IS NULL AND h.saga_state = 'completed'
                   AND jsonb_typeof(p->'path') = 'string'
            ) changed ORDER BY path, id DESC, side
        ) newest WHERE after = 'null'::jsonb ORDER BY id DESC, side, path LIMIT :most
      ) gone JOIN workstream_history h ON h.id = gone.id
     ORDER BY gone.id DESC, gone.side, gone.path
""")


def row_id(value: str) -> int | None:
    """*value* as a row's id, None when it is none."""
    return int(value) if _ROW_ID.fullmatch(value) else None


def _by(author: dict | None) -> dict:
    """Who the *author* a row's step was given is, as the wire's ``ChangedBy`` has it; you, where it was given none."""
    who = _who(author["name"], author["email"]) if author else YOU
    return {"kind": "thread", "thread_id": who["id"], "title": who["title"]} if who["kind"] == "thread" else who


def changed_by(row: WorkstreamHistory, *, picked: bool) -> dict:
    """Who a version a row records is by, as the wire's ``ChangedBy`` has it.

    A landing's files are its thread's, and what was picked up is by
    whoever its pickup step names: you, or a routine.  Each is read from the
    author its step was given.
    """
    return _by(next((s["arguments"].get("author") for s in row.steps if s["tool_name"] == _STEP[picked]), None))


def _version(row: WorkstreamHistory, entry: dict, *, picked: bool) -> dict:
    """The wire's ``FileVersion`` in snake_case of a row's *entry*, with ``blob``, its git blob id: None for a deletion."""
    merged = entry.get("merged", True)
    return {
        "id": f"{row.id}:{'p' if picked else 'f'}",
        "path": entry["path"],
        "by": changed_by(row, picked=picked),
        "at": utc(row.updated_at),
        # One that took the file away is a deletion: there is nothing of it to keep.
        "change": "deleted" if entry["after"] is None else "added" if entry["before"] is None else "changed",
        "merged": merged,
        "landing_id": str(row.id) if merged and not picked else None,
        "blob": entry["after"],
    }


async def versions(
    session_factory: Any, workstream_id: UUID | str, path: str, *, device_id: UUID | None = None, limit: int,
) -> list[dict]:
    """*path*'s newest versions, *limit* at most and newest first, from the project's records alone: who made each, when and how.

    Each is the wire's ``FileVersion`` in snake_case, with ``blob``, its git
    blob id (None for a deletion), for the caller to ask the history whether
    it is still kept.  A thread's version that did not land is listed, not
    merged; a version that landed names its landing's row.  The records are
    the cloud's, or those of a folder on the computer *device_id*.

    The last is the file's first version, when it had one before its oldest
    record: your upload, which ``main``'s first commit took as it was and
    no row records.  It is that record's ``before``, by you, and its time
    is that record's: the latest it can be.
    """
    on = [{"path": path}]
    async with session_factory() as db:
        # Each row holds a version at least, so no more rows than versions are read.
        rows = (await db.execute(
            select(WorkstreamHistory)
            .where(
                WorkstreamHistory.workstream_id == workstream_id,
                WorkstreamHistory.device_id == device_id if device_id is not None else WorkstreamHistory.device_id.is_(None),
                WorkstreamHistory.saga_state == "completed",
                or_(WorkstreamHistory.files.contains(on), WorkstreamHistory.picked_up.contains(on)),
            )
            .order_by(WorkstreamHistory.id.desc())
            .limit(limit)
        )).scalars().all()
    found = []
    first: tuple[WorkstreamHistory, dict] | None = None
    for row in rows:
        # A row's files after its pickup: the pickup came first.
        for entries, picked in ((row.files, False), (row.picked_up, True)):
            for entry in entries:
                if entry["path"] == path:
                    found.append(_version(row, entry, picked=picked))
                    first = (row, entry)
    if first is not None and first[1]["before"] is not None:
        row, entry = first
        found.append({
            "id": f"{row.id}:b", "path": path, "by": YOU, "at": utc(row.created_at), "change": "added",
            "merged": True, "landing_id": None, "blob": entry["before"],
        })
    return found[:limit]


async def version_of(session_factory: Any, workstream_id: UUID | str, version: str, path: str) -> dict | None:
    """The record of *path*'s version *version* among the project's cloud files, ``{path, after, ...}``; None when its records name none.

    A version is ``<row>:f``, one of a row's files; ``<row>:p``, one of its
    pickup; or ``<row>:b``, what the file was before that row.  The row is
    asked for as one of this project's own completed records of its cloud
    files: another project's, a computer's folder's, or one that did not
    complete, is no record here, whatever its id.  And the version is that
    row's of this very file: a row names no version of a file it did not
    change.
    """
    number, _, side = version.partition(":")
    found = row_id(number)
    if found is None or side not in ("f", "p", "b"):
        return None
    async with session_factory() as db:
        row = (await db.execute(
            select(WorkstreamHistory.files, WorkstreamHistory.picked_up).where(
                WorkstreamHistory.id == found, WorkstreamHistory.workstream_id == workstream_id,
                WorkstreamHistory.device_id.is_(None), WorkstreamHistory.saga_state == "completed",
            )
        )).first()
    if row is None:
        return None
    # Before the row: before its pickup, when it picked the file up.
    entries = row.files if side == "f" else row.picked_up if side == "p" else [*row.picked_up, *row.files]
    entry = next((e for e in entries if e["path"] == path), None)
    if side != "b" or entry is None:
        return entry
    return None if entry["before"] is None else {"path": path, "after": entry["before"]}


async def deleted_files(session_factory: Any, workstream_id: UUID | str, *, limit: int) -> tuple[list[dict], bool]:
    """The project's cloud files that are gone, *limit* at most and the newest first, and whether there are more.

    Each is the version that deleted it, as :func:`versions` gives one.  A
    file is gone when its newest landed record took it away: one made again
    since is not.  The Library lists the real files, so without these a
    deleted file would have no way to its History.  Only that many are
    read out of the records, and of each only what a version says: none of
    the rows that hold them is read whole.
    """
    async with session_factory() as db:
        found = (await db.execute(_GONE, {
            "project": workstream_id, "most": limit + 1, "commit": _STEP[False], "pickup": _STEP[True],
        })).all()
    return [
        {
            "id": f"{row}:{side}", "path": path, "by": _by(author), "at": utc(at), "change": "deleted",
            "merged": True, "landing_id": None if side == "p" else str(row), "blob": None,
        }
        for row, side, path, at, author in found[:limit]
    ], len(found) > limit
