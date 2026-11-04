"""Builtin memory tool -- delegates to the MemoryManager.

Registers the ``memory`` tool with the tool registry.  The tool schema
matches the one defined in :mod:`surogates.memory.builtin` and is
dispatched through the :class:`MemoryManager`.
"""

from __future__ import annotations

import json
from typing import Any

from surogates.channels.memory_boundary import is_case_agent_config
from surogates.tools.owner_scope import is_ops_chat_service_account
from surogates.tools.registry import ToolRegistry, ToolSchema

#: What a person's case agent is told when it saves to the ``user`` target.
_NO_FACTS_ABOUT_PEOPLE = "This session keeps no facts about people: save to the 'memory' target."


def register(registry: ToolRegistry) -> None:
    """Register the memory tool."""

    registry.register(
        name="memory",
        schema=ToolSchema(
            name="memory",
            description=(
                (
                    "Save durable facts to memory that is injected into future conversations."
                    " target='user' for who the user is and their preferences; "
                    "target='memory' for environment facts, conventions and lessons. Actions:"
                    " add (new entry), replace / remove (old_text = a short unique substring "
                    "of the entry). Save preferences and corrections when you learn them; "
                    "skip task progress, one-off details and things easy to look up again. "
                    "Only a successful call saves anything."
                )
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "replace", "remove"],
                        "description": "The action to perform.",
                    },
                    "target": {
                        "type": "string",
                        "enum": ["memory", "user"],
                        "description": "Which memory store: 'memory' for personal notes, 'user' for user profile.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The entry content. Required for 'add' and 'replace'.",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "Short unique substring identifying the entry to replace or remove.",
                    },
                },
                "required": ["action", "target"],
            },
        ),
        handler=_memory_handler,
        toolset="memory",
    )


async def _operator_name(kwargs: dict[str, Any]) -> str | None:
    """The Studio operator this session works for, when the stamp is trusted.

    Every operator of an agent shares its memory, so each entry says who it
    came from.  The ``operator`` stamp is believed only on a session whose
    account is an ops-chat one (as the prompt's identity line believes it).
    """
    operator = (kwargs.get("session_config") or {}).get("operator")
    if not isinstance(operator, dict):
        return None
    name = str(operator.get("name") or operator.get("email") or "").strip()
    if not name or kwargs.get("session_store") is None:
        return None
    tenant = kwargs.get("tenant")
    if not await is_ops_chat_service_account(
        kwargs["session_store"], getattr(tenant, "service_account_id", None),
    ):
        return None
    return name


def _labelled(content: Any, operator: str | None) -> Any:
    if not operator or not isinstance(content, str) or not content.strip():
        return content
    label = f"(noted by {operator})"
    # The first line stays the entry's title, which an add of the same title replaces.
    return content if content.rstrip().endswith(label) else f"{content.rstrip()}\n{label}"


async def _memory_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Primary memory tool -- delegates to MemoryManager or API client.

    A person's case agent keeps no facts about people: its ``user`` target is
    refused, and nothing is written.  An entry written for a Studio operator
    names them (:func:`_operator_name`).
    """
    if arguments.get("target") == "user" and is_case_agent_config(kwargs.get("session_config")):
        return json.dumps({"success": False, "error": _NO_FACTS_ABOUT_PEOPLE})

    if arguments.get("action") in ("add", "replace") and arguments.get("content"):
        arguments = {**arguments, "content": _labelled(arguments["content"], await _operator_name(kwargs))}

    # API-mediated mode: delegate to the API server.
    api_client = kwargs.get("api_client")
    if api_client is not None:
        action = arguments.get("action", "")
        target = arguments.get("target", "memory")
        content = arguments.get("content")
        old_text = arguments.get("old_text")
        return await api_client.mutate_memory(action, target, content, old_text)

    # Local mode: delegate to MemoryManager.
    memory_manager = kwargs.get("memory_manager")
    if memory_manager is not None:
        return memory_manager.handle_tool_call("memory", arguments)

    # Fallback: no manager available.
    return json.dumps({
        "success": False,
        "error": "Memory is not available. It may be disabled in config or this environment.",
    })
