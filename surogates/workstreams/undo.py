"""Restore: a project's file made one of its versions again by you, as a landing the api runs.

A Restore is a landing saga, as a thread's turn's is in its pod
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

A Restore runs to its end, though who asked for it leaves: a landing is
never left half made for a request's sake.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any
from uuid import UUID

from surogates.db.models import WorkstreamHistory
from surogates.governance.saga import SagaState, SagaStep
from surogates.harness.landing import _call, _fence, _orchestrator, _Row, _row_files, _settle, settle_running
from surogates.sandbox.history import YOU
from surogates.workstreams import stream as project_stream
from surogates.workstreams.bucket import NOT_KEPT, BucketHistory, said
from surogates.workstreams.history import (
    HISTORY_OFF,
    ProjectBusy,
    over_history_cap,
    project_lock,
    running_landings,
    start_landing,
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
CUT_SHORT = "This was cut short: the files are put back as they were when the project's files are next saved. Try again in a moment."
UNLANDED = "This file cannot be restored: the project's history keeps no file of its name."
UNTAKEN = "This file cannot be restored here: a folder of its name is there, or a file where its folder would be."
#: The landings by you under way, each kept until done: none is cut short with the request that began it.
_ACTS: set[asyncio.Future] = set()

#: What a landing by you lands, once your edits are picked up: given the copy and ``main`` as the pickup
#: left it, the applies, each ``{path, before, after}``, and the files it leaves as they are, each ``{path, by}``.
Decide = Callable[[BucketHistory, str | None], Awaitable[tuple[list[dict], list[dict]]]]


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

    async def decide(place: BucketHistory, main: str | None) -> tuple[list[dict], list[dict]]:
        # What the pickup left on main, never a fresh read: a save made since then fails the apply's check.
        recorded = await place.recorded(main, path)
        if recorded == blob:
            return [], []
        if not await place.held([blob]):
            raise Refused(NOT_KEPT, status=410)
        if recorded is None and not await place.takes(path):
            raise Refused(UNTAKEN)
        return [{"path": path, "before": recorded, "after": blob}], []

    return await _by_you(state, project, user_id, kind="restore", paths=[path], decide=decide, announce=project.master_session_id)


async def _by_you(
    state: Any, project: Any, user_id: UUID, *, kind: str, paths: list[str], decide: Decide,
    announce: UUID, undoes: list[WorkstreamHistory] = (),
) -> dict:
    """A landing by you of *kind* in *project*: your edits to *paths* picked up, then what *decide* says; ``{applied, skipped, picked_up}``.

    Under the project's lock, once the landings left running are settled.
    *decide* is given the copy and ``main`` as the pickup left it: each
    apply's ``before`` is a version ``main`` holds.  The project's stream
    tells of it as *announce*'s change: a change with no session event.
    An Undo names the rows it *undoes*.  It runs to its end, though who
    asked for it leaves.
    """
    acting = asyncio.ensure_future(_act(
        state, project, user_id, kind=kind, paths=paths, decide=decide, announce=announce, undoes=undoes,
    ))
    _ACTS.add(acting)
    acting.add_done_callback(_ACTS.discard)
    # One its request left is ended by no one: what it raises is then no one's to hear.
    acting.add_done_callback(lambda done: done.cancelled() or done.exception())
    return await asyncio.shield(acting)


async def _act(
    state: Any, project: Any, user_id: UUID, *, kind: str, paths: list[str], decide: Decide,
    announce: UUID, undoes: list[WorkstreamHistory],
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
                    state, place, project, user_id, you, audit, held,
                    kind=kind, paths=paths, decide=decide, undoes=undoes, picked=picked,
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
    *, kind: str, paths: list[str], decide: Decide, undoes: list[WorkstreamHistory], picked: list[str],
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
        applies, skipped = await decide(place, main)
    except Refused:
        raise
    except Exception as exc:
        # Before the landing's own steps, which write the files: said in words, never the server's error.
        logger.warning("A %s by you in project %s did not start", kind, project.id, exc_info=True)
        raise Refused(f"Nothing was changed. {said(exc)}" if said(exc) else UNREAD) from exc
    if applies:
        left = await _land(session_factory, place, project, user_id, you, audit, settings, held, kind=kind, main=main, applies=applies, undoes=undoes)
        if left:
            return {"applied": [], "skipped": [*skipped, *left], "picked_up": list(picked)}
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
    held: Any, *, kind: str, main: str | None, applies: list[dict], undoes: list[WorkstreamHistory],
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
        try:
            state, pushed = await _settle(saga, orchestrator, place, API, row, held=held)
        except Exception as cut:
            # Its row says where it stopped: the next lock holder puts back what it wrote.
            logger.warning("A %s by you in project %s was left for the next lock holder", kind, project.id, exc_info=True)
            raise Refused(CUT_SHORT) from cut
        if pushed is not None:
            # The push happened though its answer, or its row's last write, was lost: it landed.
            return []
        if state == "escalated":
            raise Refused(HALF) from exc
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
