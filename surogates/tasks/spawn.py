"""Spawn primitive for task-backed worker sessions.

Factored from ``surogates.tools.builtin.coordinator._spawn_worker_handler``
so the ``spawn_task`` tool (eager path) and the ``tasks_tick`` dispatcher
(deferred path) share the exact same child-session-creation logic without
duplication.

Behavioural differences vs ``spawn_worker``:

* Sets ``Session.task_id`` to the spawning Task's id so the harness can
  gate ``task_block`` on it (see ``_filter_effective_tools``).
* Uses ``channel="task"`` so the channel column distinguishes
  task-backed Sessions from plain ``spawn_worker`` children.
* Reads goal / context / agent_def_name from the Task row instead of
  tool-call kwargs.
* Raises on an unknown / disabled ``agent_def_name`` — silent fallback
  would mask config bugs and leave the orchestrator wondering why a
  specialist task is running with no specialist preset.
"""
from __future__ import annotations

import logging
from typing import Any

from surogates.harness.agent_resolver import resolve_agent_by_name
from surogates.session.events import EventType
from surogates.session.provisioning import create_child_session
from surogates.tools.builtin.coordinator import (
    WORKER_EXCLUDED_TOOLS,
    _WORKER_MAX_ITERATIONS,
)

logger = logging.getLogger(__name__)


def _build_task_worker_config(agent_def: Any | None, task: Any) -> dict[str, Any]:
    """Build the worker config dict for a task-backed Session.

    Mirrors the config-building branches in ``_spawn_worker_handler`` but
    reads ``agent_def`` + iteration ceiling from the Task row rather than
    tool-call kwargs.  Same precedence (explicit args win over agent_def
    presets) is not applicable here because the Task row has no
    "explicit tool whitelist" or "model override" channels; the agent
    def is the sole source of those presets.
    """
    child_iterations = _WORKER_MAX_ITERATIONS
    if agent_def is not None and agent_def.max_iterations is not None:
        child_iterations = min(child_iterations, agent_def.max_iterations)

    cfg: dict[str, Any] = {
        "max_iterations": child_iterations,
        "streaming": False,
    }
    if task.agent_def_name:
        cfg["agent_type"] = task.agent_def_name
    if agent_def is not None and agent_def.policy_profile:
        cfg["policy_profile"] = agent_def.policy_profile

    # Tool filter precedence: an explicit agent_def allowlist trumps
    # the default exclusion-only set.  Coordinator-family tools are
    # always stripped, even out of an allowlist, so a misconfigured
    # AgentDef cannot grant a child the right to recursively spawn.
    if agent_def is not None and agent_def.tools:
        allowed = [t for t in agent_def.tools if t not in WORKER_EXCLUDED_TOOLS]
        cfg["allowed_tools"] = allowed
    else:
        cfg["excluded_tools"] = list(WORKER_EXCLUDED_TOOLS)

    # An agent_def denylist is additive — only applied when not in
    # pure-allowlist mode (the allowlist already excluded what it wanted).
    if agent_def is not None and agent_def.disallowed_tools:
        existing = set(cfg.get("excluded_tools") or [])
        existing.update(agent_def.disallowed_tools)
        if "allowed_tools" not in cfg:
            cfg["excluded_tools"] = list(existing)

    return cfg


async def _create_session_for_task(
    task: Any,
    *,
    session_store: Any,
    session_factory: Any | None,
    tenant: Any,
) -> Any:
    """Create a Session attempt for *task*.

    Steps:

    1. Load the parent Session (to inherit workspace + identity).
    2. Resolve ``task.agent_def_name`` to an :class:`AgentDef` if set.
    3. Build the child config (max_iterations, agent_type, tool filter,
       policy_profile).
    4. Call :func:`create_child_session` with ``task_id=task.id`` and
       ``channel="task"``.
    5. Emit ``USER_MESSAGE`` on the child carrying the goal (and context
       block, when set).
    6. Emit ``WORKER_SPAWNED`` on the parent carrying ``worker_id``,
       ``task_id``, and ``goal``.

    The caller (``spawn_task`` tool handler or ``tasks_tick`` enqueue
    step) is responsible for: setting ``task.current_session_id``,
    bumping ``attempt_count``, flipping ``task.status`` to
    ``"running"``, and pushing the child Session id onto the Redis work
    queue via ``enqueue_session``.

    Raises ``ValueError`` when ``task.agent_def_name`` is set but does
    not resolve to an enabled AgentDef — silent fallback would mask
    config bugs and leave the orchestrator wondering why a specialist
    task ran with no specialist preset.
    """
    parent = await session_store.get_session(task.parent_session_id)

    agent_def = None
    if task.agent_def_name:
        agent_def = await resolve_agent_by_name(
            task.agent_def_name, tenant, session_factory=session_factory,
        )
        if agent_def is None:
            raise ValueError(
                f"Task {task.id} references agent_def_name="
                f"{task.agent_def_name!r}, but no enabled AgentDef with that "
                f"name exists in the tenant catalog."
            )

    worker_config = _build_task_worker_config(agent_def, task)

    child = await create_child_session(
        store=session_store,
        parent=parent,
        channel="task",
        model=(agent_def.model if agent_def is not None else None),
        config=worker_config,
        task_id=task.id,
    )

    # Seed the child with the goal as its first user message. With a
    # context block when set; without if not (avoids a stranded
    # '## Context' header that confuses the model on simple tasks).
    user_msg = task.goal
    if task.context:
        user_msg = f"{task.goal}\n\n## Context\n{task.context}"
    await session_store.emit_event(
        child.id, EventType.USER_MESSAGE, {"content": user_msg},
    )
    await session_store.emit_event(
        task.parent_session_id,
        EventType.WORKER_SPAWNED,
        {
            "worker_id": str(child.id),
            "task_id": str(task.id),
            "goal": task.goal,
        },
    )
    return child
