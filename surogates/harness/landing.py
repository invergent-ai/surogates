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

The saga lives in the worker's memory for the turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
from functools import partial
from typing import Any

from sqlalchemy import func, select

from surogates.governance.saga import SagaOrchestrator, SagaState, SagaStep, StepState, compensate_step
from surogates.governance.saga.compensator import compensate_history
from surogates.sandbox.pool import sandbox_session_key
from surogates.session.events import EventType

logger = logging.getLogger(__name__)

#: How long a cancelled landing waits for its put-back before the cancel goes on.
_PUT_BACK_BOUND = 300
#: Each thread's put-back still running, kept until done: a cancel never cuts one short.
_PUTTING_BACK: dict[str, asyncio.Future] = {}
#: A cancelled landing's pod going, once its slow put-back is done.
_TEARDOWNS: set[asyncio.Future] = set()


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
    repositories, files}``: *state* is ``completed``, ``compensated``
    (rolled back whole) or ``escalated`` (a put-back failed); *files* are
    the report's, every file the turn changed, ``landed`` or
    ``not_merged``; *repositories* are the folders inside a git repository
    the turn wrote into, which never land.

    The whole saga runs under the project's lock, a Postgres advisory
    transaction lock keyed by ``workstream:<id>``: one landing at a time per
    project, so a landing that starts after another sees its files as
    changed rather than rolling back over them.  It frees itself if the
    worker's connection drops.
    """
    owner = sandbox_session_key(session)
    if not sandbox_pool.holds_copy(owner):
        return None
    # Read before the lock: its holder must not wait on a second connection.
    calls = await store.get_events(session.id, after=after_event_id, types=[EventType.TOOL_CALL])
    outcome = None
    try:
        async with session_factory() as db, db.begin():
            key = f"workstream:{session.config['workstream_id']}"
            await db.execute(select(func.pg_advisory_xact_lock(func.hashtext(key))))
            outcome = await _land(sandbox_pool, session, owner, saga_settings, tool_saga_id, calls)
    except Exception:
        if outcome is None:
            raise
        # The landing is done; only the lock's transaction did not end cleanly.
        logger.warning("The project's lock for %s ended with an error", session.id, exc_info=True)
    return outcome


async def _land(
    sandbox_pool: Any, session: Any, owner: str,
    saga_settings: Any, tool_saga_id: str | None, calls: list,
) -> dict:
    orchestrator = SagaOrchestrator(**(
        {
            "default_step_timeout": saga_settings.default_step_timeout,
            "default_max_retries": saga_settings.default_max_retries,
            "retry_delay": saga_settings.retry_delay,
        } if saga_settings is not None else {}
    ))
    saga = orchestrator.create_saga(session.id)
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

    def step(name: str, **arguments: Any) -> SagaStep:
        return orchestrator.add_step(
            saga.saga_id, tool_name=f"history.{name}", tool_call_id="", arguments=arguments,
        )

    async def run(it: SagaStep) -> dict:
        action = it.tool_name.removeprefix("history.")
        raw = await sandbox_pool.execute(owner, "_history", json.dumps({**it.arguments, "action": action}))
        result = json.loads(raw)
        if "error" in result or result.get("timed_out"):
            raise LandingStepError(result.get("error") or "the pod's step timed out")
        return result

    async def execute(it: SagaStep) -> dict:
        return await orchestrator.execute_step(saga.saga_id, it.step_id, lambda: run(it))

    outcome: dict[str, Any] = {
        "saga": saga.saga_id, "state": "completed", "commit": None,
        "landed": [], "overlapped": [], "excluded": [], "repositories": [], "files": [],
    }
    changes: list[dict] = []
    try:
        turn = await execute(step("commit", author=thread, trailers=[*audit, ["Surogate-Kind", "turn"]]))
        changes = turn["changes"]
        outcome.update(overlapped=turn["overlapped"], excluded=turn["excluded"], repositories=turn["repositories"])
        if turn["commit"] is not None:
            applies = [step("apply", **change) for change in changes]
            for it in applies:
                await execute(it)
            landed = [it.execute_result for it in applies]
            recorded = await execute(step(
                "record", turn=turn["commit"], applied=landed, author=thread,
                trailers=[
                    *audit, ["Surogate-Kind", "landing"],
                    *(["Surogate-Not-Merged", o["path"]] for o in turn["overlapped"]),
                ],
            ))
            outcome.update(commit=recorded["commit"], landed=landed)
        saga.transition(SagaState.COMPLETED)
    except BaseException as exc:
        logger.warning("Landing of session %s rolled back", session.id, exc_info=True)
        # Kept, and shielded: a cancel never cuts a put-back short.
        put_back = asyncio.ensure_future(_put_back(saga, orchestrator, sandbox_pool, owner))
        _PUTTING_BACK[owner] = put_back
        put_back.add_done_callback(lambda done: _PUTTING_BACK.pop(owner, None) if _PUTTING_BACK.get(owner) is done else None)
        if not isinstance(exc, Exception):
            # Cancelled: the turn's lease went to another worker, which cannot
            # reach this pod.  What was applied still goes back, then the cancel goes on.
            await _after_cancel(put_back, sandbox_pool, owner)
            raise
        try:
            failed = await asyncio.shield(put_back)
        except asyncio.CancelledError:
            await _after_cancel(put_back, sandbox_pool, owner)
            raise
        outcome.update(state="escalated" if failed else "compensated")
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


async def _after_cancel(put_back: asyncio.Future, sandbox_pool: Any, owner: str) -> None:
    """Wait, bounded, for a cancelled landing's put-back, then let the thread's pod go.

    No later turn on this worker takes up the copy whose writes were put
    back.  A put-back still running keeps its pod, and lets it go once done.
    """
    if not await put_back_settled(owner):
        logger.warning("The cancelled landing of %s is still putting files back", owner)
        put_back.add_done_callback(lambda _: _let_go(sandbox_pool, owner))
        return
    try:
        await asyncio.shield(sandbox_pool.destroy_for_session(owner))
    except BaseException:
        logger.warning("The cancelled landing of %s did not let its pod go", owner, exc_info=True)


def _let_go(sandbox_pool: Any, owner: str) -> None:
    """Destroy *owner*'s pod in the background, kept until done."""
    going = asyncio.ensure_future(sandbox_pool.destroy_for_session(owner))
    _TEARDOWNS.add(going)
    going.add_done_callback(_TEARDOWNS.discard)


async def _put_back(saga: Any, orchestrator: SagaOrchestrator, sandbox_pool: Any, owner: str) -> list[SagaStep]:
    """Put back what a landing applied; the steps that could not be put back.

    An apply that failed, or that was cut off, may still have written its
    file: its reply was lost, or it ran out of time.  It is put back first.
    """
    failed: list[SagaStep] = []
    for it in saga.steps:
        if it.tool_name == "history.apply" and it.state in (StepState.FAILED, StepState.EXECUTING):
            try:
                await asyncio.wait_for(compensate_history(it, sandbox_pool, owner, ran=False), it.timeout_seconds)
            except Exception:
                logger.warning("Could not put back %s", it.arguments.get("path"), exc_info=True)
                failed.append(it)
    failed += await orchestrator.compensate(
        saga.saga_id, partial(compensate_step, sandbox_pool=sandbox_pool, session_id=owner),
    )
    return failed
