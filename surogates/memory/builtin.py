"""BuiltinMemoryProvider -- wraps MEMORY.md / USER.md as a MemoryProvider.

Always registered as the first provider.  Cannot be disabled or removed.

The actual storage logic lives in :mod:`surogates.memory.store`
(:class:`MemoryStore`).  This provider is a thin adapter that delegates
to MemoryStore and exposes the memory tool schema.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from surogates.memory.provider import MemoryProvider
from surogates.memory.store import MemoryStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool schema (single ``memory`` tool with action parameter)
# ---------------------------------------------------------------------------

MEMORY_TOOL_SCHEMA: dict[str, Any] = {
    "name": "memory",
    "description": (
        "Manage your persistent memory. Memory survives across sessions.\n\n"
        "Actions:\n"
        "- add: Save a new memory entry\n"
        "- replace: Update an existing entry by matching old text\n"
        "- remove: Delete an entry by matching text\n\n"
        "Targets:\n"
        "- memory: Your personal notes and learned patterns\n"
        "- user: What you know about the user (preferences, role, context)\n\n"
        "WHEN TO SAVE:\n"
        "- User preferences, corrections, or recurring patterns\n"
        "- Important project context that spans sessions\n"
        "- User's role, team, and communication style\n\n"
        "PRIORITY:\n"
        "1. Corrections to existing entries (replace)\n"
        "2. New durable facts (add)\n"
        "3. Outdated information cleanup (remove)"
    ),
    "parameters": {
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
                "default": "memory",
                "description": "Which memory store: 'memory' for personal notes, 'user' for user profile.",
            },
            "content": {
                "type": "string",
                "description": "For add: the new entry. For replace: the new content.",
            },
            "old_text": {
                "type": "string",
                "description": "For replace/remove: substring to match in existing entries.",
            },
        },
        "required": ["action"],
    },
}


class BuiltinMemoryProvider(MemoryProvider):
    """Built-in file-backed memory (MEMORY.md + USER.md).

    Always active, never disabled by other providers.
    """

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "builtin"

    async def initialize(self) -> None:
        """Load memory from disk."""
        self._store.load_from_disk()

    def system_prompt_block(self) -> str | None:
        """Return MEMORY.md and USER.md content for the system prompt.

        Uses the frozen snapshot captured at load time.
        """
        parts: list[str] = []
        mem_block = self._store.format_for_system_prompt("memory")
        if mem_block:
            parts.append(mem_block)
        user_block = self._store.format_for_system_prompt("user")
        if user_block:
            parts.append(user_block)
        return "\n\n".join(parts) if parts else None

    async def prefetch(self, query: str, session_id: str = "") -> str:
        """Built-in memory doesn't do query-based recall."""
        return ""

    async def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        session_id: str = "",
    ) -> None:
        """Built-in memory doesn't auto-sync turns."""

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Return the memory tool schema."""
        return [MEMORY_TOOL_SCHEMA]

    async def handle_tool_call(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> str:
        """Dispatch a memory tool call to the store."""
        if tool_name != "memory":
            return json.dumps({
                "success": False,
                "error": f"Unknown tool '{tool_name}' for builtin provider.",
            })

        action = args.get("action", "")
        target = args.get("target", "memory")
        content = args.get("content")
        old_text = args.get("old_text")

        if target not in ("memory", "user"):
            return json.dumps({
                "success": False,
                "error": f"Invalid target '{target}'. Use 'memory' or 'user'.",
            })

        if action == "add":
            if not content:
                return json.dumps({
                    "success": False,
                    "error": "Content is required for 'add' action.",
                })
            result = self._store.add(target, content)

        elif action == "replace":
            if not old_text:
                return json.dumps({
                    "success": False,
                    "error": "old_text is required for 'replace' action.",
                })
            if not content:
                return json.dumps({
                    "success": False,
                    "error": "content is required for 'replace' action.",
                })
            result = self._store.replace(target, old_text, content)

        elif action == "remove":
            if not old_text:
                return json.dumps({
                    "success": False,
                    "error": "old_text is required for 'remove' action.",
                })
            result = self._store.remove(target, old_text)

        else:
            return json.dumps({
                "success": False,
                "error": f"Unknown action '{action}'. Use: add, replace, remove",
            })

        return json.dumps(result, ensure_ascii=False)

    @property
    def store(self) -> MemoryStore:
        """Access the underlying MemoryStore."""
        return self._store

    async def shutdown(self) -> None:
        """No cleanup needed -- files are saved on every write."""
