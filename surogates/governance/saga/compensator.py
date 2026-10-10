"""Saga compensation strategies -- builtin (checkpoint) and MCP (undo tool).

Not present in AGT -- AGT uses a generic callable for compensation.
Surogates needs concrete strategies because compensation differs by tool
type:

* **Builtin tools** (write_file, patch, terminal) -- restore the
  filesystem snapshot via the sandbox's ``_checkpoint`` internal
  command.  The checkpoint was taken by the harness before the tool
  mutated the workspace (same mechanism used for the web UI's
  per-tool-call rollback).
* **MCP tools** -- call the undo tool declared by the MCP server
  (e.g. ``delete_jira_ticket`` to undo ``create_jira_ticket``).
* **A project's thread on its user's computer** -- put its copy there back
  to the snapshot the step began from, through the computer's own
  ``checkpoint`` kind.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from surogates.governance.saga.state_machine import Saga, SagaState, SagaStateError, SagaStep, StepState

if TYPE_CHECKING:
    from surogates.sandbox.pool import SandboxPool

logger = logging.getLogger(__name__)


async def compensate_builtin(
    step: SagaStep,
    sandbox_pool: SandboxPool,
    session_id: str,
) -> dict:
    """Compensate a builtin tool call by restoring its checkpoint.

    Sends a ``_checkpoint`` restore command through the sandbox pool,
    which is the same path the harness uses for taking snapshots.
    This works in both dev mode (ProcessSandbox, local git) and prod
    mode (K8sSandbox, git inside the pod).

    Returns the parsed restore result dict.
    """
    if not step.checkpoint_hash:
        raise SagaStateError(
            f"Step {step.step_id} ({step.tool_name}) has no checkpoint hash"
        )

    restore_input = json.dumps({
        "action": "restore",
        "hash": step.checkpoint_hash,
    })

    raw_result = await sandbox_pool.execute(
        session_id,
        "_checkpoint",
        restore_input,
    )

    try:
        result = json.loads(raw_result)
    except (json.JSONDecodeError, TypeError):
        raise SagaStateError(
            f"Checkpoint restore returned invalid JSON for step "
            f"{step.step_id}: {raw_result!r}"
        )

    if not result.get("success"):
        raise SagaStateError(
            f"Checkpoint restore failed for step {step.step_id}: "
            f"{result.get('error', 'unknown error')}"
        )

    logger.info(
        "Compensated step %s (%s) via checkpoint restore to %s",
        step.step_id, step.tool_name, step.checkpoint_hash[:8],
    )
    return result


async def compensate_mcp(
    step: SagaStep,
    sandbox_pool: SandboxPool,
    session_id: str,
) -> str:
    """Compensate an MCP tool call by invoking its declared undo tool.

    The undo tool runs in the sandbox (same as the original tool call)
    via :meth:`SandboxPool.execute`.
    """
    if not step.compensation_tool:
        raise SagaStateError(
            f"Step {step.step_id} ({step.tool_name}) has no compensation tool"
        )

    args_str = json.dumps(step.compensation_args or {})

    result = await sandbox_pool.execute(
        session_id,
        step.compensation_tool,
        args_str,
    )

    logger.info(
        "Compensated step %s (%s) via undo tool %s",
        step.step_id, step.tool_name, step.compensation_tool,
    )
    return result


async def compensate_history(
    step: SagaStep,
    sandbox_pool: SandboxPool,
    session_id: str,
    *,
    ran: bool = True,
) -> dict | None:
    """Compensate a step of a landing, in the thread's pod.

    An apply puts back the real file's version from before the landing,
    where the real file is still the one the step wrote.  *ran* is false
    for an apply that failed: it may still have written its file, and a
    file it did not write is left as it is.  The pickup, the commit and
    the record need none: your edits are picked up again by the next
    landing, the turn stays on the thread's branch, and a recorded landing
    is undone only by a new one.
    """
    if step.tool_name != "history.apply":
        return None
    # Imported here: the history's module reaches this one through the tools it imports.
    from surogates.sandbox.history import step_result

    # The folders the apply made for its file go with it; a failed apply's are not known.
    made = step.execute_result.get("made", []) if isinstance(step.execute_result, dict) else []
    # Its result, or it raises: a put-back the pod cut off, or never ran, is no put-back.
    return step_result(await sandbox_pool.execute(
        session_id, "_history", json.dumps({**step.arguments, "made": made, "action": "unapply", "ran": ran}),
    ))


#: What its person reads where a Stop of a thread on their computer did not take all of the turn back: some
#: of it stays in the thread's copy, or had already landed in the folder.  The owner's words, and these alone.
STOP_LEFT_IN_COPY = (
    "Stopped, but not all of this turn could be taken back. What the steps below changed stays in this "
    "thread's copy, and lands in your folder with the thread's next turn."
)
STOP_LEFT_LANDED = "Stopped, but part of this turn had already landed in your folder, so it was not taken back."
#: Why a step was not taken back, as its person reads it.
WHY_NOT_TAKEN_BACK = {
    "no_snapshot": "no snapshot of the copy was taken before it",
    "landed": "it had already landed in your folder",
    "not_answered": "your computer did not answer in time",
    "refused": "your computer did not put the copy back",
    "not_asked": "your computer could not be asked to put the copy back",
}


async def undo_on_computer(saga: Saga, *, copy: Any, turn: int, ran: Callable[[SagaStep], bool]) -> list[dict]:
    """Put a stopped turn's copy of a project's thread on its user's computer back to where the turn started.

    *copy* is the thread's (``surogates.devices.history.ThreadCopy``), and
    *turn* the name the turn's snapshots were taken under.  Every step that
    began, however it was left, is put back by its snapshot, newest first,
    each restore bounded by its step's timeout.  The copy is then where the
    oldest snapshot put back left it: that step and every one after it are
    taken back, those with no snapshot of their own among them.  The folder
    changes only at a landing, so nothing here reaches it.

    Answers each step before that one that is not taken back, oldest first,
    as ``{step_id, tool, why}`` and the file it named (``path``): ``why`` a
    key of :data:`WHY_NOT_TAKEN_BACK`, and ``code`` the computer's for a
    refusal.  A step that never ran (by *ran*) is not among them: it
    changed nothing.  A snapshot taken before a landing of the thread's recorded is
    on a base the copy has left: that step's work had landed.  The saga
    ends ``escalated`` where any step is not taken back, else ``completed``.
    """
    # Imported here: the devices' modules reach this one through the governance package they import.
    from surogates.devices.history import ComputerRefused, NotAnAnswer, code_of
    from surogates.devices.workspace import DeviceOperationError

    saga.transition(SagaState.COMPENSATING)
    began = [s for s in saga.steps if s.state in (StepState.EXECUTING, StepState.COMMITTED, StepState.FAILED)]
    back_from = len(began)
    why: dict[str, tuple[str, str | None]] = {}
    for index in range(len(began) - 1, -1, -1):
        step = began[index]
        if step.checkpoint_hash is None:
            continue
        try:
            await asyncio.wait_for(copy.restore(turn, step.checkpoint_hash), timeout=step.timeout_seconds)
        except TimeoutError:
            why[step.step_id] = ("not_answered", None)
        except ComputerRefused as refused:
            # The folder's history's refusal of a snapshot that is not built on the copy's base as it stands.
            why[step.step_id] = ("landed", None) if refused.code == "not_on_base" else ("refused", code_of(refused))
        except NotAnAnswer as refused:
            why[step.step_id] = ("refused", code_of(refused))
        except DeviceOperationError as refused:
            logger.warning("Step %s (%s) was not put back: %s", step.step_id, step.tool_name, refused)
            why[step.step_id] = ("not_asked", None)
        else:
            back_from = index
    left = []
    for index, step in enumerate(began):
        if index >= back_from or not ran(step):
            step.state = StepState.COMPENSATED
            continue
        step.state = StepState.COMPENSATION_FAILED
        reason, code = why.get(step.step_id, ("no_snapshot", None))
        step.error = WHY_NOT_TAKEN_BACK[reason]
        path = step.arguments.get("path") if isinstance(step.arguments, dict) else None
        left.append({
            "step_id": step.step_id, "tool": step.tool_name, "why": reason,
            **({"code": code} if code else {}), **({"path": path} if isinstance(path, str) else {}),
        })
    if left:
        saga.transition(SagaState.ESCALATED)
        saga.error = f"{len(left)} step(s) not taken back"
    else:
        saga.transition(SagaState.COMPLETED)
    logger.info(
        "Put the copy of thread %s back: %d step(s) of its turn taken back, %d not",
        saga.session_id, len(began) - back_from, len(left),
    )
    return left


def stop_left(left: list[dict]) -> dict:
    """What a Stop says where it did not take all of the turn back, as the chat draws a turn's failure: why, step by step."""
    staying = any(entry["why"] != "landed" for entry in left)
    named = [f"{entry['tool']} ({entry['path']})" if "path" in entry else entry["tool"] for entry in left]
    lines = [f"{step}: {WHY_NOT_TAKEN_BACK[entry['why']]}" for step, entry in zip(named, left)]
    return {
        "error_title": STOP_LEFT_IN_COPY if staying else STOP_LEFT_LANDED, "error_detail": "\n".join(lines),
        "error_category": "storage_error", "retryable": False, "not_taken_back": left,
    }


async def compensate_step(
    step: SagaStep,
    *,
    sandbox_pool: SandboxPool,
    session_id: str,
) -> Any:
    """Dispatch compensation for *step* based on its strategy.

    A landing's steps first, then MCP undo tool, then checkpoint restore
    (builtin tools).  An MCP step in a project's thread has both: its undo
    tool runs, and then its snapshot is restored.  Raises
    :class:`SagaStateError` if the step has no compensation strategy.
    """
    if step.tool_name.startswith("history."):
        return await compensate_history(step, sandbox_pool, session_id)

    if step.compensation_tool:
        undone = await compensate_mcp(step, sandbox_pool, session_id)
        if step.checkpoint_hash:
            await compensate_builtin(step, sandbox_pool, session_id)
        return undone

    if step.checkpoint_hash:
        return await compensate_builtin(step, sandbox_pool, session_id)

    raise SagaStateError(
        f"Step {step.step_id} ({step.tool_name}) is not compensable -- "
        "no checkpoint hash and no compensation tool defined"
    )
