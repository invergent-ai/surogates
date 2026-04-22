"""Built-in ``delegate_task`` tool -- sub-agent delegation.

Spawns a child session that runs in its own context window, waits for
it to complete, and returns the child's final LLM response as the tool
result.

The handler requires ``session_store``, ``redis``, ``tenant``,
``session_id``, and ``budget`` to be passed as keyword arguments by the
harness loop (which injects them automatically).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from surogates.config import enqueue_session
from surogates.session.events import EventType
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)

# Maximum number of seconds to wait for the child session to finish.
_DELEGATION_TIMEOUT_SECONDS: int = 300

# Polling interval when waiting for child session completion.
_POLL_INTERVAL_SECONDS: float = 1.0

# Cap on iterations that a child session may consume.
_CHILD_MAX_ITERATIONS: int = 30

# Schema for the delegate_task tool.
_DELEGATE_SCHEMA = ToolSchema(
    name="delegate_task",
    description=(
        "Delegate a task to a sub-agent that runs in its own session. "
        "Use this for complex sub-tasks that benefit from a fresh context "
        "window."
    ),
    parameters={
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "Clear description of what the sub-agent should accomplish"
                ),
            },
            "context": {
                "type": "string",
                "description": (
                    "Relevant context the sub-agent needs to know"
                ),
            },
            "model": {
                "type": "string",
                "description": (
                    "Optional model override for the sub-agent"
                ),
            },
            "agent_type": {
                "type": "string",
                "description": (
                    "Optional name of a pre-configured sub-agent type. "
                    "Applies that type's system prompt, tool filter, model, "
                    "and iteration cap to the child session.  Explicit "
                    "'model' wins over the agent type's preset."
                ),
            },
        },
        "required": ["goal"],
        "additionalProperties": False,
    },
)


def register(registry: ToolRegistry) -> None:
    """Register the ``delegate_task`` tool."""
    registry.register(
        name="delegate_task",
        schema=_DELEGATE_SCHEMA,
        handler=_delegate_handler,
        toolset="core",
    )


async def _delegate_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Create a child session, emit the task, enqueue it, and poll until complete.

    Required kwargs (injected by the harness):
        session_store   -- :class:`~surogates.session.store.SessionStore`
        redis           -- async Redis client
        tenant          -- :class:`~surogates.tenant.context.TenantContext`
        session_id      -- str, the parent session's UUID
        budget          -- :class:`~surogates.harness.budget.IterationBudget`
    """
    from uuid import UUID

    session_store = kwargs.get("session_store")
    redis = kwargs.get("redis")
    tenant = kwargs.get("tenant")
    parent_session_id_str = kwargs.get("session_id")
    budget = kwargs.get("budget")
    session_factory = kwargs.get("session_factory")

    if session_store is None:
        return json.dumps({"error": "session_store not available for delegation"})
    if tenant is None:
        return json.dumps({"error": "tenant context not available for delegation"})
    if parent_session_id_str is None:
        return json.dumps({"error": "parent session_id not available for delegation"})

    goal: str = arguments.get("goal", "")
    context: str = arguments.get("context", "")
    model_override: str | None = arguments.get("model")
    agent_type: str | None = arguments.get("agent_type")

    if not goal:
        return json.dumps({"error": "goal is required"})

    parent_session_id = UUID(str(parent_session_id_str))

    # Resolve the sub-agent type (if specified) — unknown or disabled
    # types are a hard error so the caller sees the failure.
    agent_def: Any | None = None
    if agent_type:
        from surogates.harness.agent_resolver import resolve_agent_by_name
        agent_def = await resolve_agent_by_name(
            agent_type, tenant, session_factory=session_factory,
        )
        if agent_def is None:
            return json.dumps({
                "error": (
                    f"Unknown or disabled agent_type: {agent_type!r}."
                ),
            })

    # Child inherits the parent's agent_id — delegation stays within the agent.
    parent_session = await session_store.get_session(parent_session_id)
    agent_id = parent_session.agent_id

    # Prepare the child's user message.
    user_content = goal
    if context:
        user_content = f"{goal}\n\n## Context\n{context}"

    # Check budget -- cap iterations for the child.
    # Agent def's max_iterations further narrows the cap when smaller.
    child_iterations = min(
        _CHILD_MAX_ITERATIONS,
        budget.remaining if budget else _CHILD_MAX_ITERATIONS,
    )
    if agent_def is not None and agent_def.max_iterations is not None:
        child_iterations = min(child_iterations, agent_def.max_iterations)
    if child_iterations <= 0:
        return json.dumps({"error": "iteration budget exhausted; cannot delegate"})

    # Model: explicit argument wins over agent def's preset.
    if model_override is None and agent_def is not None and agent_def.model:
        model_override = agent_def.model

    # Build the child's config — agent def supplies tool filter and
    # policy profile presets; explicit fields (none at the delegate
    # schema today) would win if present.
    child_config: dict[str, Any] = {
        "max_iterations": child_iterations,
        "streaming": False,
    }
    if agent_type:
        child_config["agent_type"] = agent_type
    if agent_def is not None:
        if agent_def.tools:
            child_config["allowed_tools"] = list(agent_def.tools)
        if agent_def.disallowed_tools and "allowed_tools" not in child_config:
            child_config["excluded_tools"] = list(agent_def.disallowed_tools)
        if agent_def.policy_profile:
            child_config["policy_profile"] = agent_def.policy_profile

    try:
        # 1. Create the child session.
        child_session = await session_store.create_session(
            user_id=tenant.user_id,
            org_id=tenant.org_id,
            agent_id=agent_id,
            channel="delegation",
            model=model_override,
            config=child_config,
            parent_id=parent_session_id,
        )
        child_id = child_session.id

        # 2. Emit a USER_MESSAGE event in the child session.
        await session_store.emit_event(
            child_id,
            EventType.USER_MESSAGE,
            {"content": user_content},
        )

        # 3. Enqueue the child session on its agent's work queue (if available).
        if redis is not None:
            await enqueue_session(redis, agent_id, child_id)

        # 4. Poll the child session's events until completion or timeout.
        result_text = await _poll_child_completion(
            session_store, child_id,
        )

        # 5. Notify memory manager of delegation outcome.
        memory_manager = kwargs.get("memory_manager")
        if memory_manager is not None:
            try:
                await memory_manager.on_delegation(
                    task=goal,
                    result=result_text[:2000],
                    child_session_id=str(child_id),
                )
            except Exception:
                logger.debug("Memory manager on_delegation failed", exc_info=True)

        return result_text

    except Exception as exc:
        logger.exception("delegate_task failed for parent %s", parent_session_id)
        return json.dumps({
            "error": f"Delegation failed: {exc}",
        })


async def _poll_child_completion(
    session_store: Any,
    child_id: Any,
    *,
    timeout: float = _DELEGATION_TIMEOUT_SECONDS,
    poll_interval: float = _POLL_INTERVAL_SECONDS,
) -> str:
    """Poll the child session's events until ``SESSION_COMPLETE`` or ``SESSION_FAIL``.

    Returns the final LLM response text from the child session, or an
    error message if the child times out or fails.
    """
    deadline = asyncio.get_event_loop().time() + timeout

    while asyncio.get_event_loop().time() < deadline:
        events = await session_store.get_events(child_id)

        for event in events:
            if event.type == EventType.SESSION_COMPLETE.value:
                from surogates.harness.message_utils import extract_final_response
                return extract_final_response(
                    events,
                    fallback=json.dumps({"error": "No response found in child session"}),
                )

            if event.type == EventType.SESSION_FAIL.value:
                error_data = event.data
                return json.dumps({
                    "error": f"Child session failed: {error_data.get('reason', 'unknown')}",
                })

        await asyncio.sleep(poll_interval)

    return json.dumps({"error": "Delegation timed out"})
