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
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from surogates.governance.saga.state_machine import SagaStateError, SagaStep

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
    file it did not write is left as it is.  The commit and the record
    need none: the turn stays on the thread's branch, and a recorded
    landing is undone only by a new one.
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
