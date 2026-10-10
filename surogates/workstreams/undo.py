"""Restore and Undo: a project's files made versions they were again by you, as landings the api runs.

A Restore makes one file one of its versions again.  An Undo puts back
what a landing replaced, or all of a thread's landings, file by file: the
compensation of a landing saga, run as a new landing.  Each is a landing
saga, as a thread's turn's is in its pod
(:mod:`surogates.harness.landing`), here over the api's own copy of the
project's history (:class:`~surogates.workstreams.bucket.BucketHistory`),
and by a landing's own rules:

- it runs under the project's lock, waited for a while at most: a person
  waits on it, and is told to try again in a moment past that;
- the landings left running are settled first, a thread's or another
  landing by you, as every lock holder settles them; one that may still be
  alive is not waited out, and the person is told to try again;
- your edits to the files it writes are picked up first: looked at, then
  pushed on ``main`` alone, as a saga of its own whose row is written
  running before the push and completed after.  Whatever stops it, the
  pickup is one a row tells of: the next lock holder finds it pushed and
  completes its row, or finds it not and nothing was written.  So the
  version a Restore replaces is a version in the file's History, or the
  file is as it was;
- then one apply a file, each checked against the version ``main`` holds
  after the pickup, never a fresh read: a save made since fails the check
  and is not written over.  Then the record, which pushes.  A step that
  still fails is put back whole.

A file an Undo would put back that is not as the landing left it, changed
since by you, by another thread or by a routine, is left as it is and named,
with who changed it: never written over.

A Restore or an Undo runs to its end, though who asked for it leaves: a
landing is never left half made for a request's sake.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from functools import partial
from typing import Any
from uuid import UUID

from sqlalchemy import select

from surogates.db.models import WorkstreamHistory
from surogates.governance.saga import SagaState, SagaStep
from surogates.harness.landing import _call, _fence, _orchestrator, _Row, _row_files, _settle, settle_running
from surogates.sandbox.history import YOU
from surogates.session.store import SessionNotFoundError
from surogates.workstreams import stream as project_stream
from surogates.workstreams.bucket import NOT_KEPT, BucketHistory, said
from surogates.workstreams.history import (
    HISTORY_OFF,
    UNDOABLE,
    ProjectBusy,
    changed_since,
    over_history_cap,
    project_lock,
    running_landings,
    start_landing,
    undone_of,
)

logger = logging.getLogger(__name__)

#: How long a landing by you, which a person waits on, waits for another landing of the project to end.
LOCK_PATIENCE = 20.0
#: The owner a step names: the api's own copy of the history, not a pod.
API = "api"
BUSY = "Your project's files are being saved right now. Try again in a moment."
UNREAD = "Nothing was changed: the project's history could not be read just now. Try again in a moment."
UNWRITTEN = "Nothing was changed: the project's files could not all be written. Try again."
HALF = "Some files could not be put back as they were. Open a file's History to restore the version you want."
WORKING = "Stop the thread to undo its changes."
UNDONE = "This change was undone already."
NO_CHANGE = "No such change."
#: The most files one Undo puts back.  Each is a step of its own under the project's lock, its edit looked
#: at first in one try of a step: past this many, a person waits longer than the page gives a landing.
UNDO_MOST = 500
#: The most files a refusal names: the rest it counts.
_NAMED_MOST = 3
CUT_SHORT = "This was cut short: the files are put back as they were when the project's files are next saved. Try again in a moment."
UNLANDED = "This file cannot be restored: the project's history keeps no file of its name."
UNTAKEN = "This file cannot be restored here: a folder of its name is there, or a file where its folder would be."
#: The landings by you under way, each kept until done: none is cut short with the request that began it.
_ACTS: set[asyncio.Future] = set()

#: What a landing by you lands, once your edits are picked up: given the copy and ``main`` as the pickup
#: left it, the applies, each ``{path, before, after}``; the files it leaves as they are, each ``{path, by}``;
#: and, by an apply's path, the landings it undoes, each with its ``id`` and ``commit``.
Decide = Callable[[BucketHistory, str | None], Awaitable[tuple[list[dict], list[dict], dict[str, list[Any]]]]]


class Refused(Exception):
    """Why a landing by you changed no file, or did not finish: said to the person as it is, with its *status*."""

    def __init__(self, words: str, *, status: int = 409) -> None:
        super().__init__(words)
        self.status = status


async def restore(state: Any, project: Any, user_id: UUID, *, path: str, blob: str) -> dict:
    """Make the real file at *path* its version *blob* again, as a landing by you: ``{applied, skipped, picked_up}``.

    It replaces whatever the file is now, which your edits' pickup records
    first (``picked_up``).  A file the version already is lands nothing.
    Refused in words for a version the history keeps no more, and a file
    the real files cannot take where a folder holds its name.  *path* is
    one a landing writes: the copy refuses any other before it reaches the
    storage, and the route says so first (:data:`UNLANDED`).
    """

    async def decide(place: BucketHistory, main: str | None) -> tuple[list[dict], list[dict], dict[str, list[Any]]]:
        # What the pickup left on main, never a fresh read: a save made since then fails the apply's check.
        recorded = (await place.recorded(main, [path]))[path]
        if recorded == blob:
            return [], [], {}
        if not await place.held([blob]):
            raise Refused(NOT_KEPT, status=410)
        if recorded is None and not await place.takes(path):
            raise Refused(UNTAKEN)
        return [{"path": path, "before": recorded, "after": blob}], [], {}

    return await _by_you(state, project, user_id, kind="restore", paths=[path], decide=decide, announce=project.master_session_id)


@dataclass(frozen=True)
class _Landed:
    """A landing an Undo may put back, as its record has it: its files that landed, each ``{path, before, after}``."""

    id: int
    commit: str
    thread_id: UUID | None
    files: tuple[dict, ...]


@dataclass(frozen=True)
class Back:
    """A file an Undo puts back to *to*, where it is still *expected*, the version the newest of *rows* left it.

    *rows* are the landings whose changes to it go back, newest first.  A
    change of another's *between* two of them, ``(older, newer)``, is one
    the file is left under, as it is.
    """

    path: str
    to: str | None
    expected: str | None
    rows: tuple[int, ...]
    between: tuple[int, int] | None = None


def _plan(landed: list[_Landed], undone: frozenset[tuple[int, str]]) -> dict[str, Back]:
    """What an Undo of *landed* puts back, file by file, each landing's changes the newest first.

    A file goes back to its version from before the oldest of them that
    changed it, as long as each found it as the one before it left it.
    Where another changed it between two of them, it is left as it is: put
    back further than that, it would lose that change, and put back less,
    it would be a version no one made.  A change an Undo put back already,
    and no later Undo brought back, is none to undo again.
    """
    plan: dict[str, Back] = {}
    for row in sorted(landed, key=lambda found: found.id, reverse=True):
        for f in row.files:
            path = f["path"]
            if (row.id, path) in undone:
                continue
            back = plan.get(path)
            if back is None:
                plan[path] = Back(path, to=f["before"], expected=f["after"], rows=(row.id,))
            elif back.between is None:
                plan[path] = (
                    replace(back, to=f["before"], rows=(*back.rows, row.id)) if f["after"] == back.to
                    else replace(back, between=(row.id, back.rows[-1]))
                )
    return dict(sorted(plan.items()))


async def undo(state: Any, project: Any, user_id: UUID, *, landing: int | None = None, thread: UUID | None = None) -> dict:
    """Undo the landing *landing*, a Restore or an Undo among them, or every landing of *thread*, as a landing by you.

    ``{applied, skipped, picked_up}``.  Each file goes back to its version
    from before the landing, or from before the thread first changed it,
    where it is still as the landing, or the thread's last landing of it,
    left it.  A file changed since is left as it is and named, with who
    changed it; one whose version from before is no longer kept is named,
    ``pruned``.  Raises :class:`LookupError` for no such change among the
    project's cloud records; and :class:`Refused` for one undone already,
    for more files than one Undo puts back, and while a thread whose
    changes these are is working: its next landing would land on top.
    """
    landed = await _undoable(state.session_factory, project, landing=landing, thread=thread)
    threads = {row.thread_id for row in landed if row.thread_id is not None} | ({thread} if thread is not None else set())
    for each in threads:
        try:
            working = (await state.session_store.get_session(each)).status == "active"
        except SessionNotFoundError:
            working = False
        if working:
            raise Refused(WORKING)
    plan = _plan(landed, await undone_of(state.session_factory, project.id))
    if not plan:
        # Nothing of it is left to put back: no landing is made, and the project's files are not waited for.
        if landing is not None:
            raise Refused(UNDONE)
        return {"applied": [], "skipped": [], "picked_up": []}
    if len(plan) > UNDO_MOST:
        raise Refused(
            f"This change has more than {UNDO_MOST:,} files to put back, more than one Undo puts back. Restore each file from its History."
            if landing is not None else
            f"This thread changed more than {UNDO_MOST:,} files, more than one Undo puts back. Undo its landings one at a time from its card."
        )

    async def decide(place: BucketHistory, main: str | None) -> tuple[list[dict], list[dict], dict[str, list[Any]]]:
        # Under the project's lock: what an Undo since put back is not undone twice.
        fresh = _plan(landed, await undone_of(state.session_factory, project.id))
        backs = [fresh[path] for path in plan if path in fresh]
        if landing is not None and not backs:
            # Another lock holder's settle, or another Undo, put it back while this one waited.
            raise Refused(UNDONE)
        # Each file as the pickup recorded it, never a fresh read: your edit to it is a version by now.
        now = await place.recorded(main, [back.path for back in backs])
        kept = await place.held(back.to for back in backs)
        by_id = {row.id: row for row in landed}
        applies, skipped, undoes = [], [], {}
        for back in backs:
            real = now[back.path]
            if back.between is None and real == back.to:
                continue
            if real != back.expected:
                skipped.append({"path": back.path, "by": await changed_since(state.session_factory, project.id, back.path, back.rows[0])})
            elif back.between is not None:
                older, newer = back.between
                skipped.append({"path": back.path, "by": await changed_since(state.session_factory, project.id, back.path, older, until_row=newer)})
            elif back.to is not None and back.to not in kept:
                skipped.append({"path": back.path, "by": None, "pruned": True})
            elif real is None and not await place.takes(back.path):
                # A folder of its name is there, or a file where its folder would be.
                skipped.append({"path": back.path, "by": await changed_since(state.session_factory, project.id, back.path, back.rows[0])})
            else:
                applies.append({"path": back.path, "before": back.expected, "after": back.to})
                undoes[back.path] = [by_id[row] for row in back.rows]
        return applies, skipped, undoes

    # One thread's changes are that thread's to tell of; any other Undo, of a landing by you, is the master's.
    announce = next(iter(threads)) if len(threads) == 1 else project.master_session_id
    return await _by_you(state, project, user_id, kind="undo", paths=list(plan), decide=decide, announce=announce)


async def _undoable(session_factory: Any, project: Any, *, landing: int | None, thread: UUID | None) -> list[_Landed]:
    """The landings an Undo of *landing*, or of *thread*'s changes, puts back: the project's own, of its cloud files.

    A landing is one that completed on ``main``: a thread's, a Restore or
    an Undo.  LookupError for any other record, whatever its id.
    """
    here = (
        WorkstreamHistory.workstream_id == project.id, WorkstreamHistory.device_id.is_(None),
        WorkstreamHistory.saga_state == "completed",
    )
    asked = (
        (WorkstreamHistory.id == landing, WorkstreamHistory.kind.in_(UNDOABLE)) if landing is not None
        else (WorkstreamHistory.thread_id == thread, WorkstreamHistory.kind == "landing")
    )
    async with session_factory() as db:
        rows = (await db.execute(
            select(WorkstreamHistory.id, WorkstreamHistory.commit, WorkstreamHistory.thread_id, WorkstreamHistory.files)
            .where(*here, *asked, WorkstreamHistory.commit.is_not(None))
            .order_by(WorkstreamHistory.id)
        )).all()
    landed = [
        _Landed(found.id, found.commit, found.thread_id, tuple(
            {"path": f["path"], "before": f["before"], "after": f["after"]} for f in found.files if f.get("merged", True)
        ))
        for found in rows
    ]
    if landing is not None and not any(row.files for row in landed):
        raise LookupError(NO_CHANGE)
    return landed


def _half(paths: list[str]) -> str:
    """What a landing by you put back only in part says: the files named, the rest counted."""
    if not paths:
        return HALF
    if len(paths) == 1:
        return f"{paths[0]} could not be put back as it was. Open its History to restore the version you want."
    named = paths[:_NAMED_MOST] if len(paths) <= _NAMED_MOST else [*paths[:_NAMED_MOST], f"{len(paths) - _NAMED_MOST} more files"]
    return f"{', '.join(named[:-1])} and {named[-1]} could not be put back as they were. Open each file's History to restore the version you want."


async def _by_you(
    state: Any, project: Any, user_id: UUID, *, kind: str, paths: list[str], decide: Decide, announce: UUID,
) -> dict:
    """A landing by you of *kind* in *project*: your edits to *paths* picked up, then what *decide* says; ``{applied, skipped, picked_up}``.

    Under the project's lock, once the landings left running are settled.
    *decide* is given the copy and ``main`` as the pickup left it: each
    apply's ``before`` is a version ``main`` holds.  The project's stream
    tells of it as *announce*'s change: a change with no session event.
    An Undo names the landings it undoes.  It runs to its end, though who
    asked for it leaves.
    """
    acting = asyncio.ensure_future(_act(
        state, project, user_id, kind=kind, paths=paths, decide=decide, announce=announce,
    ))
    _ACTS.add(acting)
    acting.add_done_callback(_ACTS.discard)
    # One its request left is ended by no one: what it raises is then no one's to hear.
    acting.add_done_callback(lambda done: done.cancelled() or done.exception())
    return await asyncio.shield(acting)


async def _act(
    state: Any, project: Any, user_id: UUID, *, kind: str, paths: list[str], decide: Decide, announce: UUID,
) -> dict:
    session_factory = state.session_factory
    master = await state.session_store.get_session(project.master_session_id)
    if await over_history_cap(state.storage, master):
        raise Refused(HISTORY_OFF)
    place = BucketHistory.of(state.storage, master, state.settings.history)
    you = {"name": str(user_id), "email": f"user:{user_id}@surogate"}
    audit = [["Surogate-Project", str(project.id)], ["Surogate-Agent", str(project.agent_id)], ["Surogate-User", str(user_id)]]
    picked: list[str] = []
    answer: dict | None = None
    refused: Refused | None = None
    try:
        async with project_lock(session_factory, project.id, patience=LOCK_PATIENCE) as held:
            try:
                answer = await _landed(
                    state, place, project, user_id, you, audit, held, kind=kind, paths=paths, decide=decide, picked=picked,
                )
            except Refused as exc:
                refused = exc
    except ProjectBusy as exc:
        raise Refused(BUSY) from exc
    except Exception as exc:
        if answer is None and refused is None:
            logger.warning("A %s by you in project %s did not start", kind, project.id, exc_info=True)
            raise Refused(UNREAD) from exc
        # It is done; only the lock's transaction did not end cleanly.
        logger.warning("The project's lock for a %s by you in project %s ended with an error", kind, project.id, exc_info=True)
    if picked or (answer is not None and answer["applied"]):
        # Your edit is a version now, whatever came of the rest: the project's files changed.
        await project_stream.publish(state.redis, project.id, announce, project_stream.LANDED)
    if refused is not None:
        raise refused
    return answer


async def _landed(
    state: Any, place: BucketHistory, project: Any, user_id: UUID, you: dict, audit: list, held: Any,
    *, kind: str, paths: list[str], decide: Decide, picked: list[str],
) -> dict:
    """The landing by you under the project's lock, *held*: settled, picked up, decided, landed."""
    session_factory, settings = state.session_factory, state.settings.saga
    # A landing still within its fence may yet finish: a person is not kept waiting on it.
    if any(quiet < _fence(settings) for _, quiet in await running_landings(session_factory, project.id)):
        raise Refused(BUSY)
    try:
        # A landing it completes is told to the project's stream as it is settled: its row's files changed.
        await settle_running(
            session_factory, place, API, project.id, settings, held, redis=state.redis, master=project.master_session_id,
        )
        main = await _pick_up(session_factory, place, project, user_id, you, audit, paths, settings, held, picked)
        applies, skipped, undoes = await decide(place, main)
    except Refused:
        raise
    except Exception as exc:
        # Before the landing's own steps, which write the files: said in words, never the server's error.
        logger.warning("A %s by you in project %s did not start", kind, project.id, exc_info=True)
        raise Refused(f"Nothing was changed. {said(exc)}" if said(exc) else UNREAD) from exc
    for tried in range(2):
        if not applies:
            break
        undid = sorted({row.id: row for a in applies for row in undoes.get(a["path"], ())}.values(), key=lambda row: row.id)
        left = await _land(session_factory, place, project, user_id, you, audit, settings, held, kind=kind, main=main, applies=applies, undoes=undid)
        if not left:
            break
        if tried:
            # Saved over again as it was tried once more: put back whole, and nothing of it written.
            raise Refused(UNWRITTEN)
        # As a thread's landing leaves out a file changed since, the rest is tried once more at once, as a landing of its own.
        saved = {each["path"] for each in left}
        skipped, applies = [*skipped, *left], [a for a in applies if a["path"] not in saved]
    return {"applied": [a["path"] for a in applies], "skipped": skipped, "picked_up": list(picked)}


async def _pick_up(
    session_factory: Any, place: BucketHistory, project: Any, user_id: UUID, you: dict, audit: list, paths: list[str],
    settings: Any, held: Any, picked: list[str],
) -> str | None:
    """Your edits to *paths*, pushed on ``main`` alone as a pickup by you, with its row; ``main`` after it.

    Looked at first, then a saga of its own: its row is written running,
    its one step naming what it pushes and the ``main`` it pushes on,
    before anything is pushed, and completed after.  Each path recorded
    is added to *picked*.  Raises when it was not pushed with a row that
    says so: nothing is moved past a pickup no row tells of.
    """
    orchestrator = _orchestrator(settings)
    looked = await orchestrator.attempt(partial(place.edits, paths))
    if not looked["picked_up"]:
        return looked["main"]
    saga = orchestrator.create_saga(project.master_session_id, kind="landing")
    pickup = orchestrator.add_step(saga.saga_id, tool_name="history.pickup", tool_call_id="", arguments={
        "main": looked["main"], "picked_up": looked["picked_up"], "author": you,
        "trailers": [*audit, ["Surogate-Saga", saga.saga_id], ["Surogate-Kind", "pickup"]],
    })
    row = _Row(session_factory, await start_landing(
        session_factory, saga, workstream_id=project.id, thread_id=None, agent_id=str(project.agent_id),
        user_id=user_id, tool_saga_id=None, events=None, kind="pickup",
    ), saga)

    async def run() -> dict:
        # Each try marks the row alive first: another lock holder's fence runs from here.
        await row.alive()
        return await _call(place, API, "pickup", **pickup.arguments)

    try:
        # A lock lost unseen frees the project: nothing is pushed.
        await held()
        pushed = (await orchestrator.execute_step(saga.saga_id, pickup.step_id, run))["commit"]
        saga.transition(SagaState.COMPLETED)
        await row.write(state="completed", commit=pushed, picked_up=pickup.arguments["picked_up"])
    except Exception:
        logger.warning("Your edits in project %s were not picked up", project.id, exc_info=True)
        # Its answer may have been lost, or its row's last write: the history says whether it pushed.
        _, pushed = await _settle(saga, orchestrator, place, API, row, held=held)
        if pushed is None or row.state != "completed":
            raise
    picked.extend(change["path"] for change in pickup.arguments["picked_up"])
    return pushed


async def _land(
    session_factory: Any, place: BucketHistory, project: Any, user_id: UUID, you: dict, audit: list, settings: Any,
    held: Any, *, kind: str, main: str | None, applies: list[dict], undoes: list[Any],
) -> list[dict]:
    """The saga of a landing by you: one apply a file, then the record; put back whole if a step fails.

    The files it left as they are, each ``{path, by}``: none when it
    landed.  Put back, a file saved since the pickup is one, by you, and
    nothing of the landing is written.

    Its row is written running with its steps fixed, the record among
    them, before the first apply: all a put-back by another lock holder
    needs.  It is written again before the record's first try, with what
    each apply did, so that a push is one its row can list.
    """
    orchestrator = _orchestrator(settings)
    saga = orchestrator.create_saga(project.master_session_id, kind="landing")
    steps = [orchestrator.add_step(saga.saga_id, tool_name="history.apply", tool_call_id="", arguments=a) for a in applies]
    record = orchestrator.add_step(saga.saga_id, tool_name="history.record", tool_call_id="", arguments={
        "applied": applies, "author": you, "main": main,
        "trailers": [
            *audit, ["Surogate-Saga", saga.saga_id], ["Surogate-Kind", kind], *(["Surogate-Undoes", r.commit] for r in undoes),
        ],
    })
    try:
        row = _Row(session_factory, await start_landing(
            session_factory, saga, workstream_id=project.id, thread_id=None, agent_id=str(project.agent_id),
            user_id=user_id, tool_saga_id=None, events=None, kind=kind, undoes=[r.id for r in undoes] or None,
        ), saga)
    except Exception as exc:
        logger.warning("A %s by you in project %s did not start", kind, project.id, exc_info=True)
        raise Refused(UNREAD) from exc

    async def run(it: SagaStep) -> dict:
        # Each try marks the row alive first: another lock holder's fence runs from here.
        await row.alive()
        return await _call(place, API, it.tool_name.removeprefix("history."), **it.arguments)

    try:
        for it in steps:
            # A lock lost unseen frees the project: the landing stops, and is put back.
            await held()
            await orchestrator.execute_step(saga.saga_id, it.step_id, partial(run, it))
        await row.write()
        await held()
        await orchestrator.execute_step(saga.saga_id, record.step_id, partial(run, record))
        saga.transition(SagaState.COMPLETED)
        await row.write(state="completed", commit=record.execute_result["commit"], files=_row_files(saga, "completed"))
    except Exception as exc:
        logger.warning("A %s by you in project %s did not finish", kind, project.id, exc_info=True)
        # The files a put-back left as the landing wrote them, to name.
        at_issue: list[str] = []
        try:
            state, pushed = await _settle(saga, orchestrator, place, API, row, held=held, at_issue=at_issue)
        except Exception as cut:
            # Its row says where it stopped: the next lock holder puts back what it wrote.
            logger.warning("A %s by you in project %s was left for the next lock holder", kind, project.id, exc_info=True)
            raise Refused(CUT_SHORT) from cut
        if pushed is not None:
            # The push happened though its answer, or its row's last write, was lost: it landed.
            return []
        if state == "escalated":
            raise Refused(_half(at_issue)) from exc
        # A file saved since your edit was picked up is left as it is, as a thread's landing leaves out one
        # changed since: said to have been changed after this, by you.
        left = await _changed_since(place, applies)
        if left:
            return [{"path": path, "by": YOU} for path in left]
        # A bound's refusal, as a version too large to write, is said as it is.
        raise Refused(f"Nothing was changed. {said(exc)}" if said(exc) else UNWRITTEN) from exc
    return []


async def _changed_since(place: BucketHistory, applies: list[dict]) -> list[str]:
    """Those of *applies*' files that are no longer the version each was to replace; none where it cannot be told."""
    try:
        return [apply["path"] for apply in applies if await place.real(apply["path"]) != apply["before"]]
    except Exception:
        logger.warning("Could not tell whether the files of a landing by you changed since its pickup", exc_info=True)
        return []
