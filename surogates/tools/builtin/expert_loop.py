"""Expert mini agent loop -- runs a scoped LLM loop using an expert model.

When the base LLM calls ``consult_expert``, this module executes a
bounded agent loop using the expert's model and endpoint.  The expert
gets its own restricted tool set (declared in ``expert_tools``) and a
capped iteration budget (``expert_max_iterations``).  Tool calls go
through the normal governance and routing pipeline.

The loop terminates when the expert produces a text response (no tool
calls) or when the iteration budget is exhausted.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from surogates.tools.loader import SkillDef

logger = logging.getLogger(__name__)

# Maximum characters for a single tool result in the expert's context.
_MAX_TOOL_RESULT_CHARS: int = 20_000


class ExpertBudgetExceeded(Exception):
    """Raised when an expert exhausts its iteration budget."""

    def __init__(self, expert_name: str, max_iterations: int) -> None:
        self.expert_name = expert_name
        self.max_iterations = max_iterations
        super().__init__(
            f"Expert '{expert_name}' exhausted its iteration budget "
            f"({max_iterations} iterations)"
        )


async def run_expert_loop(
    *,
    expert: SkillDef,
    task: str,
    context: str | None,
    tool_router: Any,
    tool_registry: Any,
    tenant: Any,
    session_id: UUID,
    session_store: Any | None = None,
) -> tuple[str, int]:
    """Run a scoped agent loop using the expert's configured model.

    Parameters
    ----------
    expert:
        The expert :class:`SkillDef` (must have ``is_active_expert == True``).
    task:
        The task description from the base LLM's ``consult_expert`` call.
    context:
        Optional context string provided by the base LLM.
    tool_router:
        The :class:`~surogates.tools.router.ToolRouter` for dispatching
        tool calls (governance still applies).
    tool_registry:
        The :class:`~surogates.tools.registry.ToolRegistry` for schema
        export.
    tenant:
        The :class:`~surogates.tenant.context.TenantContext`.
    session_id:
        The current session UUID.
    session_store:
        Optional :class:`~surogates.session.store.SessionStore` for
        emitting expert events.

    Returns
    -------
    tuple[str, int]
        A ``(result_text, iterations_used)`` pair.

    Raises
    ------
    ExpertBudgetExceeded
        If the expert does not produce a final response within
        ``expert.expert_max_iterations`` iterations.
    """
    from openai import AsyncOpenAI

    from surogates.session.events import EventType

    # Build the scoped tool set -- only tools declared in the expert's config.
    allowed_tools = set(expert.expert_tools) if expert.expert_tools else set()
    tool_schemas = tool_registry.get_schemas(names=allowed_tools) if allowed_tools else []

    # Build expert messages with system prompt + user task.
    system_prompt = _build_expert_system_prompt(expert)
    user_content = _build_user_message(task, context)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    # Create a client for the expert's endpoint.
    client = AsyncOpenAI(
        base_url=expert.expert_endpoint,
        api_key=_resolve_api_key(tenant, expert),
    )

    try:
        for iteration in range(expert.expert_max_iterations):
            # Call the expert model.
            create_kwargs: dict[str, Any] = {
                "model": expert.expert_model or "default",
                "messages": messages,
            }
            if tool_schemas:
                create_kwargs["tools"] = tool_schemas

            response = await client.chat.completions.create(**create_kwargs)
            choice = response.choices[0]
            message = choice.message

            # If no tool calls, the expert is done.
            if not message.tool_calls:
                return message.content or "", iteration + 1

            # Append the assistant message (with tool calls) to context.
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": message.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in message.tool_calls
                ],
            }
            messages.append(assistant_msg)

            # Execute each tool call through the normal router.
            for tc in message.tool_calls:
                tool_name = tc.function.name

                # Enforce scoping: reject tool calls outside allowed set.
                if allowed_tools and tool_name not in allowed_tools:
                    result = json.dumps({
                        "error": f"Tool '{tool_name}' is not available to this expert. "
                        f"Available tools: {', '.join(sorted(allowed_tools))}",
                    })
                else:
                    result = await tool_router.execute(
                        name=tool_name,
                        arguments=tc.function.arguments,
                        tenant=tenant,
                        session_id=session_id,
                    )

                # Truncate large tool results to avoid blowing the expert's context.
                result_str = str(result)
                if len(result_str) > _MAX_TOOL_RESULT_CHARS:
                    result_str = result_str[:_MAX_TOOL_RESULT_CHARS] + "\n[truncated]"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_str,
                })

        # Budget exhausted without a final text response.
        raise ExpertBudgetExceeded(expert.name, expert.expert_max_iterations)

    finally:
        await client.close()


def _build_expert_system_prompt(expert: SkillDef) -> str:
    """Build the system prompt for the expert's mini-loop.

    Uses the SKILL.md body as the primary instructions, with a preamble
    identifying the expert's role and constraints.
    """
    parts: list[str] = [
        f"You are an expert assistant specialised in: {expert.description}",
        "",
        "You are running as a sub-agent inside a larger system. Complete the "
        "given task using your available tools and return your final answer as "
        "text. Be concise and precise.",
    ]

    if expert.expert_tools:
        parts.append(
            f"\nYou have access to these tools: {', '.join(expert.expert_tools)}. "
            "Do not attempt to call tools not in this list."
        )

    # Append the skill body as additional instructions.
    if expert.content:
        parts.append(f"\n# Expert Instructions\n{expert.content}")

    return "\n".join(parts)


def _build_user_message(task: str, context: str | None) -> str:
    """Build the user message for the expert from the task and optional context."""
    if context:
        return f"{task}\n\n## Context\n{context}"
    return task


def _resolve_api_key(tenant: Any, expert: SkillDef) -> str:
    """Resolve the API key for the expert's endpoint.

    Checks the tenant's org config for expert-specific credentials,
    falling back to a default key for self-hosted endpoints that don't
    require authentication.
    """
    org_config = getattr(tenant, "org_config", {}) or {}

    # Check org config for expert-specific API key.
    expert_keys = org_config.get("expert_api_keys", {})
    if isinstance(expert_keys, dict):
        key = expert_keys.get(expert.name)
        if key:
            return str(key)

    # Check for a global expert API key.
    global_key = org_config.get("expert_api_key")
    if global_key:
        return str(global_key)

    # Self-hosted endpoints (vLLM, Ollama) typically don't need auth.
    return "not-needed"
