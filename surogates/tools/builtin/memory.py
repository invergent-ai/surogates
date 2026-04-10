"""Builtin memory tool -- delegates to the MemoryManager.

Registers the ``memory`` tool with the tool registry.  The tool schema
matches the one defined in :mod:`surogates.memory.builtin` and is
dispatched through the :class:`MemoryManager`.
"""

from __future__ import annotations

import json
from typing import Any

from surogates.tools.registry import ToolRegistry, ToolSchema


def register(registry: ToolRegistry) -> None:
    """Register the memory tool."""

    registry.register(
        name="memory",
        schema=ToolSchema(
            name="memory",
            description=(
                "Save durable information to persistent memory that survives across sessions. "
                "Memory is injected into future turns, so keep it compact and focused on facts "
                "that will still matter later.\n\n"
                "WHEN TO SAVE (do this proactively, don't wait to be asked):\n"
                "- User corrects you or says 'remember this' / 'don't do that again'\n"
                "- User shares a preference, habit, or personal detail (name, role, timezone, coding style)\n"
                "- You discover something about the environment (OS, installed tools, project structure)\n"
                "- You learn a convention, API quirk, or workflow specific to this user's setup\n"
                "- You identify a stable fact that will be useful again in future sessions\n\n"
                "PRIORITY: User preferences and corrections > environment facts > procedural knowledge. "
                "The most valuable memory prevents the user from having to repeat themselves.\n\n"
                "Do NOT save task progress, session outcomes, completed-work logs, or temporary TODO "
                "state to memory; use session_search to recall those from past transcripts.\n"
                "If you've discovered a new way to do something, solved a problem that could be "
                "necessary later, save it as a skill with the skill tool.\n\n"
                "TWO TARGETS:\n"
                "- 'user': who the user is -- name, role, preferences, communication style, pet peeves\n"
                "- 'memory': your notes -- environment facts, project conventions, tool quirks, lessons learned\n\n"
                "ACTIONS: add (new entry), replace (update existing -- old_text identifies it), "
                "remove (delete -- old_text identifies it).\n\n"
                "SKIP: trivial/obvious info, things easily re-discovered, raw data dumps, and temporary task state."
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


async def _memory_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Primary memory tool -- delegates to MemoryManager."""
    memory_manager = kwargs.get("memory_manager")
    if memory_manager is not None:
        return memory_manager.handle_tool_call("memory", arguments)

    # Fallback: no manager available.
    return json.dumps({
        "success": False,
        "error": "Memory is not available. It may be disabled in config or this environment.",
    })
