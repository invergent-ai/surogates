"""Built-in coordinator tools -- parallel worker session management.

Provides three tools for coordinator sessions:

- ``spawn_worker`` -- create a child session and enqueue it for processing.
  Returns immediately with the worker's session ID (non-blocking).
- ``send_worker_message`` -- send a follow-up message to a running or
  completed worker, waking it if idle.
- ``stop_worker`` -- interrupt a running worker via Redis pub/sub.

Workers are full sessions with their own event logs, leases, budgets,
and sandboxes.  They are processed by the standard orchestrator and
can run on any worker pod.  When a worker completes, a
``WORKER_COMPLETE`` event is emitted into the parent session's event
log and the parent is re-enqueued so it wakes up to process the result.

All three tools validate ownership: the caller's session must be the
parent of the target worker (``parent_id`` check).
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from surogates.config import INTERRUPT_CHANNEL_PREFIX, WORK_QUEUE_KEY
from surogates.session.events import EventType
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Cap on iterations that a worker session may consume.
_WORKER_MAX_ITERATIONS: int = 30

# Tools that workers are NOT allowed to use (prevents recursive spawning).
WORKER_EXCLUDED_TOOLS: frozenset[str] = frozenset({
    "spawn_worker",
    "send_worker_message",
    "stop_worker",
})

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_SPAWN_WORKER_SCHEMA = ToolSchema(
    name="spawn_worker",
    description=(
        "Spawn a new worker to execute a task in its own session. "
        "The worker runs asynchronously — this tool returns immediately "
        "with the worker's ID. Results arrive as worker.complete events "
        "in your next turn. Launch multiple workers in parallel by "
        "calling spawn_worker multiple times in the same response."
    ),
    parameters={
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "Complete, self-contained description of what the worker "
                    "should accomplish. Workers cannot see your conversation — "
                    "include all necessary context, file paths, and specifics."
                ),
            },
            "context": {
                "type": "string",
                "description": "Additional context the worker needs",
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional whitelist of tool names the worker may use. "
                    "If omitted, the worker gets all tools except coordinator tools."
                ),
            },
            "model": {
                "type": "string",
                "description": "Optional model override for the worker",
            },
        },
        "required": ["goal"],
        "additionalProperties": False,
    },
)

_SEND_WORKER_MESSAGE_SCHEMA = ToolSchema(
    name="send_worker_message",
    description=(
        "Send a follow-up message to an existing worker. Use this to "
        "continue a worker that completed its task (it has full context "
        "from its previous run), correct a failing worker, or extend "
        "a worker's assignment. The worker wakes up and processes the "
        "message in its next turn."
    ),
    parameters={
        "type": "object",
        "properties": {
            "worker_id": {
                "type": "string",
                "description": "The worker's session ID (from spawn_worker result)",
            },
            "message": {
                "type": "string",
                "description": "The follow-up message to send",
            },
        },
        "required": ["worker_id", "message"],
        "additionalProperties": False,
    },
)

_STOP_WORKER_SCHEMA = ToolSchema(
    name="stop_worker",
    description=(
        "Stop a running worker. The worker is interrupted and will "
        "report a partial result. You can continue a stopped worker "
        "with send_worker_message."
    ),
    parameters={
        "type": "object",
        "properties": {
            "worker_id": {
                "type": "string",
                "description": "The worker's session ID to stop",
            },
            "reason": {
                "type": "string",
                "description": "Why the worker is being stopped",
            },
        },
        "required": ["worker_id"],
        "additionalProperties": False,
    },
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(registry: ToolRegistry) -> None:
    """Register the coordinator tools."""
    registry.register(
        name="spawn_worker",
        schema=_SPAWN_WORKER_SCHEMA,
        handler=_spawn_worker_handler,
        toolset="core",
    )
    registry.register(
        name="send_worker_message",
        schema=_SEND_WORKER_MESSAGE_SCHEMA,
        handler=_send_worker_message_handler,
        toolset="core",
    )
    registry.register(
        name="stop_worker",
        schema=_STOP_WORKER_SCHEMA,
        handler=_stop_worker_handler,
        toolset="core",
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _spawn_worker_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Create a child session, emit the goal, and enqueue for processing.

    Returns immediately with the worker's session ID.

    Required kwargs (injected by the harness):
        session_store   -- SessionStore
        redis           -- async Redis client
        tenant          -- TenantContext
        session_id      -- str, the parent session's UUID
        budget          -- IterationBudget
    """
    session_store = kwargs.get("session_store")
    redis = kwargs.get("redis")
    tenant = kwargs.get("tenant")
    parent_session_id_str = kwargs.get("session_id")
    budget = kwargs.get("budget")

    if session_store is None:
        return json.dumps({"error": "session_store not available"})
    if tenant is None:
        return json.dumps({"error": "tenant context not available"})
    if parent_session_id_str is None:
        return json.dumps({"error": "parent session_id not available"})

    goal: str = arguments.get("goal", "")
    context: str = arguments.get("context", "")
    model_override: str | None = arguments.get("model")
    tool_whitelist: list[str] | None = arguments.get("tools")

    if not goal:
        return json.dumps({"error": "goal is required"})

    parent_session_id = UUID(str(parent_session_id_str))

    # Inherit the parent's agent_id.
    parent_session = await session_store.get_session(parent_session_id)
    agent_id = parent_session.agent_id

    # Build the worker's user message.
    user_content = goal
    if context:
        user_content = f"{goal}\n\n## Context\n{context}"

    # Budget: worker gets a slice of the parent's remaining budget.
    child_iterations = min(
        _WORKER_MAX_ITERATIONS,
        budget.remaining if budget else _WORKER_MAX_ITERATIONS,
    )
    if child_iterations <= 0:
        return json.dumps({"error": "iteration budget exhausted; cannot spawn worker"})

    # Build worker config.
    worker_config: dict[str, Any] = {
        "max_iterations": child_iterations,
        "streaming": False,
    }
    # Tool whitelist — stored in session config and enforced by the harness.
    if tool_whitelist is not None:
        # Strip coordinator-only tools from the whitelist.
        allowed = [t for t in tool_whitelist if t not in WORKER_EXCLUDED_TOOLS]
        worker_config["allowed_tools"] = allowed
    else:
        # Default: exclude coordinator tools so workers cannot spawn sub-workers.
        worker_config["excluded_tools"] = list(WORKER_EXCLUDED_TOOLS)

    # Inherit workspace path so the worker can access the same files.
    workspace_path = parent_session.config.get("workspace_path")
    if workspace_path:
        worker_config["workspace_path"] = workspace_path

    try:
        # 1. Create the child session.
        child_session = await session_store.create_session(
            user_id=tenant.user_id,
            org_id=tenant.org_id,
            agent_id=agent_id,
            channel="worker",
            model=model_override,
            config=worker_config,
            parent_id=parent_session_id,
        )
        child_id = child_session.id

        # 2. Emit a USER_MESSAGE event in the child session.
        await session_store.emit_event(
            child_id,
            EventType.USER_MESSAGE,
            {"content": user_content},
        )

        # 3. Emit WORKER_SPAWNED event in the parent session.
        await session_store.emit_event(
            parent_session_id,
            EventType.WORKER_SPAWNED,
            {
                "worker_id": str(child_id),
                "goal": goal[:500],
            },
        )

        # 4. Enqueue the child session to the work queue.
        if redis is not None:
            await redis.zadd(WORK_QUEUE_KEY, {str(child_id): 0})

        return json.dumps({
            "worker_id": str(child_id),
            "status": "spawned",
            "goal": goal[:200],
            "iterations": child_iterations,
        })

    except Exception as exc:
        logger.exception("spawn_worker failed for parent %s", parent_session_id)
        return json.dumps({"error": f"Failed to spawn worker: {exc}"})


async def _send_worker_message_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Send a follow-up message to a worker and re-enqueue it.

    Required kwargs (injected by the harness):
        session_store   -- SessionStore
        redis           -- async Redis client
        session_id      -- str, the parent session's UUID
    """
    session_store = kwargs.get("session_store")
    redis = kwargs.get("redis")
    parent_session_id_str = kwargs.get("session_id")

    if session_store is None:
        return json.dumps({"error": "session_store not available"})
    if parent_session_id_str is None:
        return json.dumps({"error": "parent session_id not available"})

    worker_id_str: str = arguments.get("worker_id", "")
    message: str = arguments.get("message", "")

    if not worker_id_str:
        return json.dumps({"error": "worker_id is required"})
    if not message:
        return json.dumps({"error": "message is required"})

    try:
        worker_id = UUID(worker_id_str)
    except ValueError:
        return json.dumps({"error": f"Invalid worker_id: {worker_id_str}"})

    parent_session_id = UUID(str(parent_session_id_str))

    # Validate ownership: worker must be a child of the caller.
    try:
        worker_session = await session_store.get_session(worker_id)
    except Exception:
        return json.dumps({"error": f"Worker session not found: {worker_id_str}"})

    if worker_session.parent_id != parent_session_id:
        return json.dumps({"error": "Worker does not belong to this session"})

    try:
        # Reset session status to active if it was completed/failed,
        # so the harness processes the new message on wake.
        if worker_session.status in ("completed", "failed"):
            await session_store.update_session_status(worker_id, "active")

        # Emit user message into the worker's event log.
        await session_store.emit_event(
            worker_id,
            EventType.USER_MESSAGE,
            {"content": message},
        )

        # Re-enqueue the worker so it wakes up.
        if redis is not None:
            await redis.zadd(WORK_QUEUE_KEY, {str(worker_id): 0})

        return json.dumps({
            "status": "sent",
            "worker_id": worker_id_str,
        })

    except Exception as exc:
        logger.exception("send_worker_message failed for worker %s", worker_id)
        return json.dumps({"error": f"Failed to send message: {exc}"})


async def _stop_worker_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Interrupt a running worker via Redis pub/sub.

    Required kwargs (injected by the harness):
        session_store   -- SessionStore
        redis           -- async Redis client
        session_id      -- str, the parent session's UUID
    """
    session_store = kwargs.get("session_store")
    redis = kwargs.get("redis")
    parent_session_id_str = kwargs.get("session_id")

    if session_store is None:
        return json.dumps({"error": "session_store not available"})
    if parent_session_id_str is None:
        return json.dumps({"error": "parent session_id not available"})

    worker_id_str: str = arguments.get("worker_id", "")
    reason: str = arguments.get("reason", "stopped by coordinator")

    if not worker_id_str:
        return json.dumps({"error": "worker_id is required"})

    try:
        worker_id = UUID(worker_id_str)
    except ValueError:
        return json.dumps({"error": f"Invalid worker_id: {worker_id_str}"})

    parent_session_id = UUID(str(parent_session_id_str))

    # Validate ownership.
    try:
        worker_session = await session_store.get_session(worker_id)
    except Exception:
        return json.dumps({"error": f"Worker session not found: {worker_id_str}"})

    if worker_session.parent_id != parent_session_id:
        return json.dumps({"error": "Worker does not belong to this session"})

    try:
        # Publish interrupt via Redis pub/sub.
        if redis is not None:
            channel = f"{INTERRUPT_CHANNEL_PREFIX}:{worker_id}"
            await redis.publish(channel, json.dumps({"reason": reason}))

        return json.dumps({
            "status": "stop_requested",
            "worker_id": worker_id_str,
            "reason": reason,
        })

    except Exception as exc:
        logger.exception("stop_worker failed for worker %s", worker_id)
        return json.dumps({"error": f"Failed to stop worker: {exc}"})
