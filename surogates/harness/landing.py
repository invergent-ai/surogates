"""Landing: a project thread's turn applied to the project's real files.

A landing is one saga, run by the worker at the turn's end, its steps
calls of the pod's ``_history`` command: commit the turn on the thread's
branch, apply each file, record the landing on ``main``.  The files to
apply are fixed before the first apply.  A step that still fails after its
retries rolls the landing back: the applied files are put back, in reverse
order, and so is the file of an apply that failed, which may have written
it before its reply was lost.  The real files are then as they were.  All
or nothing.

A file the real files changed since the thread's branch point is left out
by decision, not by failure: the newer file stays, and the thread's
version is kept in history as the landing's second parent.

The saga's durable record is its ``workstream_history`` row, written as it
runs.  The record step is the push of the project's history, the moment a
landing counts: a landing counts only once ``main`` in the history carries
its saga, and one that does is never put back.  The next holder of the
project's lock settles a landing a killed worker left running before it
does anything else.
"""

from __future__ import annotations

import asyncio
import json
import logging
from functools import partial
from typing import Any

from surogates.governance.saga import SagaOrchestrator, SagaState, SagaStep, StepState, compensate_step
from surogates.governance.saga.compensator import compensate_history
from surogates.governance.saga.orchestrator import (
    SAGA_DEFAULT_MAX_RETRIES,
    SAGA_DEFAULT_RETRY_DELAY_SECONDS,
    SAGA_DEFAULT_STEP_TIMEOUT_SECONDS,
)
from surogates.sandbox.pool import sandbox_session_key
from surogates.session.events import EventType
from surogates.workstreams.history import (
    project_lock,
    running_landings,
    saga_of,
    save_landing,
    start_landing,
    touch_landing,
)

logger = logging.getLogger(__name__)

#: How long a cancelled landing waits for its put-back before the cancel goes on.
_PUT_BACK_BOUND = 300
#: Each thread's put-back still running, kept until done: a cancel never cuts one short.
_PUTTING_BACK: dict[str, asyncio.Future] = {}
#: A cancelled landing's pod going, once its slow put-back is done.
_TEARDOWNS: set[asyncio.Future] = set()


def putting_back(owner: str) -> bool:
    """Whether a landing of *owner* is still putting files back; it lets its pod go itself once done."""
    return owner in _PUTTING_BACK


async def put_back_settled(owner: str) -> bool:
    """Whether no landing of *owner* is still putting files back, waiting within its bound for one."""
    pending = _PUTTING_BACK.get(owner)
    if pending is None:
        return True
    try:
        await asyncio.wait_for(asyncio.shield(pending), _PUT_BACK_BOUND)
    except BaseException:
        return False
    return True


class LandingStepError(RuntimeError):
    """A ``_history`` step answered with an error."""


async def _call(sandbox_pool: Any, owner: str, action: str, **arguments: Any) -> dict:
    """One ``_history`` action in *owner*'s pod; an error answer raises."""
    result = json.loads(await sandbox_pool.execute(owner, "_history", json.dumps({**arguments, "action": action})))
    if "error" in result or result.get("timed_out"):
        raise LandingStepError(result.get("error") or "the pod's step timed out")
    return result


async def land_turn(
    *,
    store: Any,
    session_factory: Any,
    sandbox_pool: Any,
    session: Any,
    saga_settings: Any,
    tool_saga_id: str | None,
    after_event_id: int,
) -> dict | None:
    """Land *session*'s turn; its outcome, or None when the turn never used its pod.

    The outcome is ``{saga, state, commit, landed, overlapped, excluded,
    repositories, files, saved}``: *state* is ``completed``, ``compensated``
    (rolled back whole) or ``escalated`` (a put-back failed); *files* are
    the report's, every file the turn changed, ``landed`` or
    ``not_merged``; *repositories* are the folders inside a git repository
    the turn wrote into, which never land.

    The whole saga runs under the project's lock: one landing at a time per
    project, so a landing that starts after another sees its files as
    changed rather than rolling back over them.  The lock frees itself if
    its connection drops, so the landing asks it before each apply and the
    record, and stops when it is gone.  The landings a killed worker left
    running are settled first; this thread's own, if one had pushed, is
    reported with this turn's files.
    """
    owner = sandbox_session_key(session)
    if not sandbox_pool.holds_copy(owner):
        return None
    # Read before the lock: its holder must not wait on a second connection.
    calls = await store.get_events(session.id, after=after_event_id, types=[EventType.TOOL_CALL])
    workstream = session.config["workstream_id"]
    outcome = None
    try:
        async with project_lock(session_factory, workstream) as held:
            settled = await settle_running(session_factory, sandbox_pool, owner, workstream, saga_settings)
            outcome = await _land(session_factory, sandbox_pool, session, owner, saga_settings, tool_saga_id, calls, held)
    except Exception:
        if outcome is None:
            raise
        # The landing is done; only the lock's transaction did not end cleanly.
        logger.warning("The project's lock for %s ended with an error", session.id, exc_info=True)
    known = {f["ref"] for f in outcome["files"]}
    outcome["files"] += [
        # A landing of this thread a killed worker had pushed: its files landed, and the report says so.
        {"kind": "file", "label": f["path"], "ref": f["path"], "landing": "landed",
         **({"change": "deleted"} if f["after"] is None else {})}
        for row in settled if row["thread"] == session.id and row["state"] == "completed"
        for f in row["files"] if f["merged"] and f["path"] not in known
    ]
    return outcome


async def keep_copy(*, session_factory: Any, sandbox_pool: Any, session: Any, saga_settings: Any) -> dict | None:
    """Keep *session*'s copy on its thread's branch, under the project's lock; None when it holds none.

    A thread's failed turn keeps its work, base and all, to land with its
    next turn: its files may be half made.  The landings a killed worker
    left running are settled first, as every lock holder does.
    """
    owner = sandbox_session_key(session)
    if not sandbox_pool.holds_copy(owner):
        return None
    workstream = session.config["workstream_id"]
    author = {"name": session.title or "Thread", "email": f"thread:{session.id}@surogate"}
    trailers = [
        ["Surogate-Project", str(workstream)], ["Surogate-Thread", str(session.id)],
        ["Surogate-Agent", str(session.agent_id)], ["Surogate-User", str(session.user_id)],
        ["Surogate-Kind", "turn"],
    ]
    async with project_lock(session_factory, workstream):
        await settle_running(session_factory, sandbox_pool, owner, workstream, saga_settings)
        return await _call(sandbox_pool, owner, "keep", author=author, trailers=trailers, base=True)


def _fence(saga_settings: Any) -> float:
    """The longest a live landing goes without writing its row, and a second more.

    Each try of a step writes it first, so that is a try and the wait
    before the next; the looks outside the steps have a try's bound too.
    """
    timeout, retries, delay = (
        (saga_settings.default_step_timeout, saga_settings.default_max_retries, saga_settings.retry_delay)
        if saga_settings is not None else
        (SAGA_DEFAULT_STEP_TIMEOUT_SECONDS, SAGA_DEFAULT_MAX_RETRIES, SAGA_DEFAULT_RETRY_DELAY_SECONDS)
    )
    return timeout + delay * retries + 1


def _orchestrator(saga_settings: Any) -> SagaOrchestrator:
    return SagaOrchestrator(**(
        {
            "default_step_timeout": saga_settings.default_step_timeout,
            "default_max_retries": saga_settings.default_max_retries,
            "retry_delay": saga_settings.retry_delay,
        } if saga_settings is not None else {}
    ))


async def _land(
    session_factory: Any, sandbox_pool: Any, session: Any, owner: str,
    saga_settings: Any, tool_saga_id: str | None, calls: list, held: Any,
) -> dict:
    orchestrator = _orchestrator(saga_settings)
    saga = orchestrator.create_saga(session.id, kind="landing")
    thread = {"name": session.title or "Thread", "email": f"thread:{session.id}@surogate"}
    audit = [
        ["Surogate-Project", str(session.config["workstream_id"])],
        ["Surogate-Thread", str(session.id)],
        ["Surogate-Agent", str(session.agent_id)],
        ["Surogate-User", str(session.user_id)],
        ["Surogate-Saga", saga.saga_id],
        *([["Surogate-Tool-Saga", tool_saga_id]] if tool_saga_id else []),
        *([["Surogate-Events", f"{calls[0].id}-{calls[-1].id}"]] if calls else []),
    ]
    row = await start_landing(
        session_factory, saga, workstream_id=session.config["workstream_id"], thread_id=session.id,
        agent_id=str(session.agent_id), user_id=session.user_id, tool_saga_id=tool_saga_id,
        events=(calls[0].id, calls[-1].id) if calls else None,
    )
    save = partial(save_landing, session_factory, row, saga)

    def step(name: str, **arguments: Any) -> SagaStep:
        return orchestrator.add_step(
            saga.saga_id, tool_name=f"history.{name}", tool_call_id="", arguments=arguments,
        )

    async def run(it: SagaStep) -> dict:
        # Each try marks the row alive first: another lock holder's fence runs from here.
        await touch_landing(session_factory, row)
        return await _call(sandbox_pool, owner, it.tool_name.removeprefix("history."), **it.arguments)

    async def execute(it: SagaStep) -> dict:
        try:
            return await orchestrator.execute_step(saga.saga_id, it.step_id, lambda: run(it))
        finally:
            # Each step's state, as it ends, in the saga's row.
            await save()

    outcome: dict[str, Any] = {
        "saga": saga.saga_id, "state": "completed", "commit": None,
        "landed": [], "overlapped": [], "excluded": [], "repositories": [], "files": [],
        # Whether the turn's work is in the history: its commit step pushed
        # it, and held no file, whose version the next copy would lack.
        "saved": False,
    }
    changes: list[dict] = []
    main: str | None = None
    try:
        commit = step("commit", author=thread, trailers=[*audit, ["Surogate-Kind", "turn"]])
        # The first look, outside the steps: it changes nothing, and under
        # the lock no one else moves main until this landing is done.
        main = (await asyncio.wait_for(_call(sandbox_pool, owner, "fetch"), commit.timeout_seconds))["main"]
        turn = await execute(commit)
        changes = turn["changes"]
        outcome.update(
            overlapped=turn["overlapped"], excluded=turn["excluded"], repositories=turn["repositories"],
            saved=not turn["overlapped"],
        )
        if turn["commit"] is not None:
            applies = [step("apply", **change) for change in changes]
            await save()
            for it in applies:
                # A lock lost unseen frees the project: this landing stops, and is put back.
                await held()
                await execute(it)
            landed = [it.execute_result for it in applies]
            record = step(
                "record", turn=turn["commit"], applied=landed, author=thread, main=main,
                trailers=[
                    *audit, ["Surogate-Kind", "landing"],
                    *(["Surogate-Not-Merged", o["path"]] for o in turn["overlapped"]),
                ],
            )
            await save()
            await held()
            recorded = await execute(record)
            outcome.update(commit=recorded["commit"], landed=landed)
        saga.transition(SagaState.COMPLETED)
        await save(state="completed", commit=outcome["commit"], files=_row_files(saga, "completed"))
    except BaseException as exc:
        logger.warning("Landing of session %s did not finish", session.id, exc_info=True)
        # Kept, and shielded: a cancel never cuts a put-back short.
        put_back = asyncio.ensure_future(_settle(saga, orchestrator, sandbox_pool, owner, save))
        _PUTTING_BACK[owner] = put_back
        put_back.add_done_callback(lambda done: _PUTTING_BACK.pop(owner, None) if _PUTTING_BACK.get(owner) is done else None)
        if not isinstance(exc, Exception):
            # Cancelled: the turn's lease went to another worker, which cannot
            # reach this pod.  What was applied still goes back, then the cancel goes on.
            await _after_cancel(put_back, sandbox_pool, owner)
            raise
        try:
            state, pushed = await asyncio.shield(put_back)
        except asyncio.CancelledError:
            await _after_cancel(put_back, sandbox_pool, owner)
            raise
        outcome.update(state=state)
        if pushed is not None:
            # The push happened though its answer was lost: the landing counts.
            outcome.update(commit=pushed, landed=[s.execute_result for s in saga.steps if s.tool_name == "history.apply"])
    applied = {c["path"]: c for c in outcome["landed"]}
    reasons = {o["path"]: o["reason"] for o in outcome["overlapped"]}
    paths = sorted({c["path"] for c in changes} | set(reasons))
    outcome["files"] = [
        {
            "kind": "file", "label": path, "ref": path,
            "landing": "landed" if path in applied else "not_merged",
            # A landed deletion is no file to open: the report names it apart.
            **({"change": "deleted"} if path in applied and applied[path]["after"] is None else {}),
            # Why a file was left out, for the report's line on it.
            **({"reason": reasons[path]} if path in reasons else {}),
        }
        for path in paths
    ]
    return outcome


def _row_files(saga: Any, state: str) -> list[dict]:
    """A landing row's files: each it landed, and each it left out, with its two versions; none unless it completed."""
    if state != "completed":
        return []
    commit = next((s.execute_result for s in saga.steps if s.tool_name == "history.commit"), None) or {}
    landed = [s.execute_result for s in saga.steps if s.tool_name == "history.apply" and s.state is StepState.COMMITTED]
    return [
        *({"path": c["path"], "before": c["before"], "after": c["after"], "merged": True} for c in landed),
        *({"path": o["path"], "before": o["before"], "after": o["after"], "merged": False} for o in commit.get("overlapped", [])),
    ]


async def _settle(
    saga: Any, orchestrator: SagaOrchestrator, sandbox_pool: Any, owner: str, save: Any,
    *, recovered: bool = False,
) -> tuple[str, str | None]:
    """End a landing that did not finish: ``completed`` with its commit when it pushed, else put back.

    It pushed only when ``main`` in the history carries its saga.  ``main``
    moved without it means another landing went first, with this one's lock
    lost, or a command rewrote the history: never that this one pushed.  A
    *recovered* landing's steps are as its row last had them: a step it
    was in shows ``pending``.
    """
    record = next((s for s in saga.steps if s.tool_name == "history.record"), None)
    # Whatever its state: a try that pushed shows ``pending`` again in its retry's wait.
    if record is not None:
        looked = await asyncio.wait_for(_call(sandbox_pool, owner, "fetch", saga=saga.saga_id), record.timeout_seconds)
        if looked["has_saga"]:
            if saga.state is SagaState.RUNNING:
                saga.transition(SagaState.COMPLETED)
            await _written(save, tries=2, state="completed", commit=looked["main"], files=_row_files(saga, "completed"))
            return "completed", looked["main"]
    failed = await _put_back(saga, orchestrator, sandbox_pool, owner, save, recovered=recovered)
    state = "escalated" if failed else "compensated"
    await _written(save, tries=2, state=state)
    return state, None


async def _written(save: Any, *, tries: int = 1, **values: Any) -> None:
    """Write a landing's row, as best it can: an outcome already known stands without it.

    A write that fails is caught up by the next, or by the next lock
    holder's settle, which puts the files back again: that is safe to repeat.
    """
    for _ in range(tries):
        try:
            return await save(**values)
        except Exception:
            logger.warning("A landing's row was not written", exc_info=True)


async def settle_running(
    session_factory: Any, sandbox_pool: Any, owner: str, workstream_id: Any, saga_settings: Any,
) -> list[dict]:
    """Settle the project's landings left running, through *owner*'s pod; each ``{thread, state, files}``.

    A row written within the fence is waited for: its worker may still be
    in a step, or putting files back past its bound.
    """
    fence = _fence(saga_settings)
    settled = []
    while running := await running_landings(session_factory, workstream_id):
        row, quiet = running[0]
        if quiet < fence:
            await asyncio.sleep(fence - quiet)
            continue
        saga = saga_of(row)
        orchestrator = _orchestrator(saga_settings)
        orchestrator.adopt(saga)
        committed = next((s.execute_result for s in saga.steps if s.tool_name == "history.commit" and s.state is StepState.COMMITTED), None)
        if committed is not None and committed["commit"] is not None:
            # The turn and its base: the versions a put-back writes.
            await _call(sandbox_pool, owner, "fetch", commits=[c for c in (committed["commit"], committed["base"]) if c])
        state, _ = await _settle(
            saga, orchestrator, sandbox_pool, owner, partial(save_landing, session_factory, row.id, saga), recovered=True,
        )
        logger.warning("Settled landing %s of %s, left running: %s", row.saga_id, row.thread_id, state)
        settled.append({"thread": row.thread_id, "state": state, "files": _row_files(saga, state)})
    return settled


async def _after_cancel(put_back: asyncio.Future, sandbox_pool: Any, owner: str) -> None:
    """Wait, bounded, for a cancelled landing's put-back, then let the thread's pod go.

    No later turn on this worker takes up the copy whose writes were put
    back.  A put-back still running keeps its pod, and lets it go once done.
    """
    if not await put_back_settled(owner):
        logger.warning("The cancelled landing of %s is still putting files back", owner)
        waited_on = sandbox_pool.sandbox_of(owner)
        put_back.add_done_callback(lambda _: _let_go(sandbox_pool, owner, waited_on))
        return
    try:
        await asyncio.shield(sandbox_pool.destroy_for_session(owner))
    except BaseException:
        logger.warning("The cancelled landing of %s did not let its pod go", owner, exc_info=True)


def _let_go(sandbox_pool: Any, owner: str, sandbox_id: str | None) -> None:
    """Destroy *owner*'s pod *sandbox_id* in the background, kept until done; a later turn's pod stays."""
    going = asyncio.ensure_future(_destroy_if_still(sandbox_pool, owner, sandbox_id))
    _TEARDOWNS.add(going)
    going.add_done_callback(_TEARDOWNS.discard)


async def _destroy_if_still(sandbox_pool: Any, owner: str, sandbox_id: str | None) -> None:
    if sandbox_id is not None and (released := await sandbox_pool.release_for_session(owner, only=sandbox_id)):
        await sandbox_pool.destroy_released(released, owner)


async def _put_back(
    saga: Any, orchestrator: SagaOrchestrator, sandbox_pool: Any, owner: str, save: Any,
    *, recovered: bool = False,
) -> list[SagaStep]:
    """Put back what a landing applied; the steps that could not be put back.

    An apply that failed, or that was cut off, may still have written its
    file: its reply was lost, or it ran out of time.  It is put back first,
    only where the real file is the turn's version.  A *recovered*
    landing's apply still ``pending`` may have too: the kill came before its
    state was written.  The saga's row follows each put-back.
    """
    unsure = (StepState.FAILED, StepState.EXECUTING, *((StepState.PENDING,) if recovered else ()))
    failed: list[SagaStep] = []
    for it in saga.steps:
        if it.tool_name == "history.apply" and it.state in unsure:
            try:
                await asyncio.wait_for(compensate_history(it, sandbox_pool, owner, ran=False), it.timeout_seconds)
            except Exception:
                logger.warning("Could not put back %s", it.arguments.get("path"), exc_info=True)
                failed.append(it)
            await _written(save)

    async def compensate(it: SagaStep) -> Any:
        try:
            return await compensate_step(it, sandbox_pool=sandbox_pool, session_id=owner)
        finally:
            await _written(save)

    failed += await orchestrator.compensate(saga.saga_id, compensate)
    return failed
