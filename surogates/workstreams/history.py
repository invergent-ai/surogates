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
import json
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
from surogates.workstreams.derive import undone_files, utc

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


class ProjectBusy(RuntimeError):
    """Another landing held the project's lock for longer than its waiter's patience."""


@asynccontextmanager
async def project_lock(
    session_factory: Any, workstream_id: UUID | str, *, patience: float | None = None,
) -> AsyncIterator[Callable[[], Awaitable[None]]]:
    """Hold *workstream_id*'s lock for the block, on a connection of its own; a check that it is still held.

    Tried, not waited for in Postgres: a landing waiting for the lock holds
    no pooled connection between its tries.  The lock goes with its
    connection, unseen, so the check asks that connection to answer.  With
    *patience*, :class:`ProjectBusy` once that many seconds of tries have
    failed: a person waits on a landing of theirs.
    """
    key = func.hashtext(f"workstream:{workstream_id}")
    deadline = None if patience is None else time.monotonic() + patience
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
        if deadline is not None and time.monotonic() >= deadline:
            raise ProjectBusy(f"project {workstream_id}'s files are being changed")
        await asyncio.sleep(LOCK_POLL)


async def start_landing(
    session_factory: Any, saga: Saga, *, workstream_id: UUID | str, thread_id: UUID | None, agent_id: str,
    user_id: UUID | None, tool_saga_id: str | None, events: tuple[int, int] | None,
    kind: str = "landing", undoes: list[int] | None = None, device_id: UUID | None = None, folder: str | None = None,
) -> int:
    """The row of a landing about to take its first step, the saga ``running``; its id.

    *kind* is ``landing`` for a thread's.  A landing by you has no thread:
    ``pickup`` for your edits, pushed alone before a file of them is
    written over, and ``restore`` or ``undo`` for the landing that follows;
    an Undo names the rows it *undoes*.  A landing in *folder* on the
    computer *device_id* names both (:mod:`surogates.harness.local_landing`);
    neither, it is of the project's cloud files.
    """
    async with session_factory() as db, db.begin():
        return (await db.execute(
            insert(WorkstreamHistory).values(
                workstream_id=workstream_id, kind=kind, saga_id=saga.saga_id, saga_state="running",
                steps=saga.to_dict()["steps"], thread_id=thread_id, tool_saga_id=tool_saga_id,
                events=Range(events[0], events[1], bounds="[]") if events else None,
                agent_id=agent_id, user_id=user_id, undoes=undoes, device_id=device_id, folder=folder,
            ).returning(WorkstreamHistory.id)
        )).scalar_one()


async def landing_row(session_factory: Any, saga_id: str) -> WorkstreamHistory | None:
    """The row of the landing *saga_id*, as it was last written; None where it has none.

    A landing on a computer is named after its turn's invocation, so a turn
    taken up again after its worker was lost finds the row its first run made.
    """
    async with session_factory() as db:
        return (await db.execute(select(WorkstreamHistory).where(WorkstreamHistory.saga_id == saga_id))).scalar_one_or_none()


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
    """The landings of the project's cloud files still ``running``, oldest first, each with the seconds since its row last changed.

    A landing in a folder on a computer is that folder's to settle: no pod
    and no lock of the project's reaches it, and a pod that took one would
    put its files back in the cloud's.
    """
    quiet = func.extract("epoch", func.now() - WorkstreamHistory.updated_at)
    async with session_factory() as db:
        rows = await db.execute(
            select(WorkstreamHistory, quiet)
            .where(
                WorkstreamHistory.workstream_id == workstream_id, WorkstreamHistory.saga_state == "running",
                WorkstreamHistory.device_id.is_(None),
            )
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
#: The kinds of row whose files a person can undo: each a landing on ``main``.
UNDOABLE = ("landing", "restore", "undo")
#: A row's id as a version or a landing names it: digits the column holds, never what a guess could overflow.
_ROW_ID = re.compile(r"[0-9]{1,18}")
#: The step whose author a version is by: what a row picked up is its pickup's; a thread's landing's files are
#: its commit's, and the files of a landing by you, which has no turn to commit, its record's.
_PICKUP, _COMMIT, _RECORD = "history.pickup", "history.commit", "history.record"
#: How a version came to be, for a landing by you: by its row's kind.
_MADE = {"restore": "restored", "undo": "undone"}
#: The most of a project's latest records its deleted files are looked for among: what the list costs
#: is bounded whatever the project's age.  A file deleted before them is listed no more.
_GONE_AMONG = 10_000
# The project's cloud files that are gone, looked for among its latest :among records: each one's newest landed
# record, where that took the file away, the newest first and :most of them, with who that record's step says it
# is by: its pickup's, its commit's, or the record's of a landing by you.  A row's files come after its pickup.
# One row at least, with how many records were looked among.
_GONE = text("""
    WITH recent AS (
        SELECT id, files, picked_up FROM workstream_history
         WHERE workstream_id = :project AND device_id IS NULL AND saga_state = 'completed'
         ORDER BY created_at DESC, id DESC LIMIT :among
    ), changed AS (
        SELECT r.id, 'f' AS side, f->>'path' AS path, f->'after' AS after
          FROM recent r, jsonb_array_elements(r.files) f
         WHERE jsonb_typeof(f->'path') = 'string' AND f->'merged' IS DISTINCT FROM 'false'::jsonb
        UNION ALL
        SELECT r.id, 'p', p->>'path', p->'after'
          FROM recent r, jsonb_array_elements(r.picked_up) p
         WHERE jsonb_typeof(p->'path') = 'string'
    ), newest AS (
        SELECT DISTINCT ON (path) id, side, path, after FROM changed ORDER BY path, id DESC, side
    ), gone AS (
        SELECT id, side, path FROM newest WHERE after = 'null'::jsonb ORDER BY id DESC, side, path LIMIT :most
    )
    SELECT among.records, gone.id, gone.side, gone.path, h.updated_at, (
               SELECT s->'arguments'->'author' FROM jsonb_array_elements(h.steps) s
                WHERE s->>'tool_name' = CASE WHEN gone.side = 'p' THEN :pickup WHEN h.kind = 'landing' THEN :commit ELSE :record END
                LIMIT 1
           ) AS author
      FROM (SELECT count(*) AS records FROM recent) among
      LEFT JOIN gone ON true
      LEFT JOIN workstream_history h ON h.id = gone.id
     ORDER BY gone.id DESC, gone.side, gone.path
""")
# Who last changed :path after the record :after and before the files of the record :until: the newest of the
# project's cloud records that changed it, its files before its pickup, with who that record's step says it is by.
# A landing that left the file out did not change it.
_SINCE = text("""
    SELECT h.id, changed.side, (
               SELECT s->'arguments'->'author' FROM jsonb_array_elements(h.steps) s
                WHERE s->>'tool_name' = CASE WHEN changed.side = 'p' THEN :pickup WHEN h.kind = 'landing' THEN :commit ELSE :record END
                LIMIT 1
           ) AS author
      FROM workstream_history h
     CROSS JOIN LATERAL (
            SELECT 'f' AS side WHERE h.id < :until AND EXISTS (
                SELECT 1 FROM jsonb_array_elements(h.files) f
                 WHERE f->>'path' = :path AND f->'merged' IS DISTINCT FROM 'false'::jsonb
            )
            UNION ALL
            SELECT 'p' WHERE EXISTS (SELECT 1 FROM jsonb_array_elements(h.picked_up) p WHERE p->>'path' = :path)
           ) changed
     WHERE h.workstream_id = :project AND h.device_id IS NULL AND h.saga_state = 'completed'
       AND h.id > :after AND h.id <= :until AND (h.files @> cast(:on AS jsonb) OR h.picked_up @> cast(:on AS jsonb))
     ORDER BY h.id DESC, changed.side
     LIMIT 1
""")
#: The newest a record's id can be: what :func:`changed_since` reads up to, unless told.
_NEWEST = 2**62


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
    whoever its pickup step names: you, or a routine.  A Restore's files
    are by whoever its record names: you.  Each is read from the author
    its step was given.
    """
    step = _PICKUP if picked else _COMMIT if row.kind == "landing" else _RECORD
    return _by(next((s["arguments"].get("author") for s in row.steps if s["tool_name"] == step), None))


def _version(row: WorkstreamHistory, entry: dict, *, picked: bool) -> dict:
    """The wire's ``FileVersion`` in snake_case of a row's *entry*, with ``blob``, its git blob id: None for a deletion."""
    merged = entry.get("merged", True)
    return {
        "id": f"{row.id}:{'p' if picked else 'f'}",
        "path": entry["path"],
        "by": changed_by(row, picked=picked),
        "at": utc(row.updated_at),
        # One that took the file away is a deletion: there is nothing of it to keep.
        "change": (
            "deleted" if entry["after"] is None else _MADE[row.kind] if not picked and row.kind in _MADE
            else "added" if entry["before"] is None else "changed"
        ),
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
    merged; a version that landed names its landing's row, to undo, until
    an Undo put that file back.  The records are the cloud's, or those of a
    folder on the computer *device_id*.

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
    # A change an Undo put back already is none to undo again from its History.
    undone = await undone_of(session_factory, workstream_id, device_id=device_id) if rows else frozenset()
    found = []
    first: tuple[WorkstreamHistory, dict] | None = None
    for row in rows:
        # A row's files after its pickup: the pickup came first.
        for entries, picked in ((row.files, False), (row.picked_up, True)):
            for entry in entries:
                if entry["path"] == path:
                    version = _version(row, entry, picked=picked)
                    if (row.id, path) in undone:
                        version["landing_id"] = None
                    found.append(version)
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
    """The project's cloud files that are gone, *limit* at most and the newest first, and whether there may be more.

    Each is the version that deleted it, as :func:`versions` gives one.  A
    file is gone when its newest landed record took it away: one made again
    since is not.  The Library lists the real files, so without these a
    deleted file would have no way to its History.  They are looked for
    among the project's latest records, a bounded number of them, and only
    that many are read out, of each only what a version says: none of the
    rows that hold them is read whole.  There may be more where more were
    found than are listed, and where the project has records older than
    those looked among.
    """
    async with session_factory() as db:
        answered = (await db.execute(_GONE, {
            "project": workstream_id, "among": _GONE_AMONG, "most": limit + 1, "pickup": _PICKUP, "commit": _COMMIT, "record": _RECORD,
        })).all()
    found = [found for found in answered if found.id is not None]
    return [
        {
            "id": f"{row}:{side}", "path": path, "by": _by(author), "at": utc(at), "change": "deleted",
            "merged": True, "landing_id": None if side == "p" else str(row), "blob": None,
        }
        for _, row, side, path, at, author in found[:limit]
    ], len(found) > limit or answered[0].records >= _GONE_AMONG


async def undone_of(session_factory: Any, workstream_id: UUID | str, *, device_id: UUID | None = None) -> frozenset[tuple[int, str]]:
    """Each ``(row, path)`` of the project's records an Undo put back, and no later Undo brought back (:func:`undone_files`).

    Read from the project's completed Undos of its cloud files, or of a
    folder on the computer *device_id*.
    """
    async with session_factory() as db:
        undos = (await db.execute(
            select(WorkstreamHistory.id, WorkstreamHistory.undoes, WorkstreamHistory.files).where(
                WorkstreamHistory.workstream_id == workstream_id,
                WorkstreamHistory.device_id == device_id if device_id is not None else WorkstreamHistory.device_id.is_(None),
                WorkstreamHistory.kind == "undo", WorkstreamHistory.saga_state == "completed",
            )
        )).all()
    return undone_files({"id": found, "undoes": undoes, "files": files} for found, undoes, files in undos)


async def changed_since(
    session_factory: Any, workstream_id: UUID | str, path: str, after_row: int, *, until_row: int | None = None,
) -> dict:
    """Who last changed *path* after the record *after_row*, as the wire's ``ChangedBy`` has it; you, where no record says.

    The newest of the project's cloud records that changed it: a landing
    that left the file out did not.  With *until_row*, only those before
    that record's own files: its pickup came before them.  An edit of
    yours a pickup has not recorded yet is yours.
    """
    async with session_factory() as db:
        found = (await db.execute(_SINCE, {
            "project": workstream_id, "path": path, "on": json.dumps([{"path": path}]), "after": after_row,
            "until": _NEWEST if until_row is None else until_row, "pickup": _PICKUP, "commit": _COMMIT, "record": _RECORD,
        })).first()
    return YOU if found is None else _by(found.author)
