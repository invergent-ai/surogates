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

    def is_available(self) -> bool:
        """Built-in memory is always available."""
        return True

    def initialize(self, session_id: str = "", **kwargs) -> None:
        """Load memory from disk."""
        self._store.load_from_disk()

    def system_prompt_block(self) -> str:
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
        return "\n\n".join(parts)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Built-in memory doesn't do query-based recall."""
        return ""

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """Built-in memory doesn't auto-sync turns."""

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Return the memory tool schema."""
        return [MEMORY_TOOL_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
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

    def shutdown(self) -> None:
        """No cleanup needed -- files are saved on every write."""

    @property
    def store(self) -> MemoryStore:
        """Access the underlying MemoryStore."""
        return self._store
