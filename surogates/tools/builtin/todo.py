"""Builtin todo tool -- in-memory task management per session.

Registers the ``todo`` tool with the tool registry.  Provides a
per-session in-memory task list for decomposing complex tasks,
tracking progress, and maintaining focus across long conversations.

Design:
- Single ``todo`` tool: provide ``todos`` param to write, omit to read
- Every call returns the full current list
- No system prompt mutation, no tool response modification
- Behavioral guidance lives entirely in the tool schema description
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)

# Valid status values for todo items
VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}


class TodoStore:
    """In-memory todo list. One instance per session.

    Items are ordered -- list position is priority. Each item has:
      - id: unique string identifier (agent-chosen)
      - content: task description
      - status: pending | in_progress | completed | cancelled
    """

    def __init__(self) -> None:
        self._items: List[Dict[str, str]] = []

    def write(self, todos: List[Dict[str, Any]], merge: bool = False) -> List[Dict[str, str]]:
        """Write todos. Returns the full current list after writing.

        Args:
            todos: list of {id, content, status} dicts
            merge: if False, replace the entire list. If True, update
                   existing items by id and append new ones.
        """
        if not merge:
            # Replace mode: new list entirely
            self._items = [self._validate(t) for t in todos]
        else:
            # Merge mode: update existing items by id, append new ones
            existing = {item["id"]: item for item in self._items}
            for t in todos:
                item_id = str(t.get("id", "")).strip()
                if not item_id:
                    continue  # Can't merge without an id

                if item_id in existing:
                    # Update only the fields the LLM actually provided
                    if "content" in t and t["content"]:
                        existing[item_id]["content"] = str(t["content"]).strip()
                    if "status" in t and t["status"]:
                        status = str(t["status"]).strip().lower()
                        if status in VALID_STATUSES:
                            existing[item_id]["status"] = status
                else:
                    # New item -- validate fully and append to end
                    validated = self._validate(t)
                    existing[validated["id"]] = validated
                    self._items.append(validated)
            # Rebuild _items preserving order for existing items
            seen: set[str] = set()
            rebuilt: List[Dict[str, str]] = []
            for item in self._items:
                current = existing.get(item["id"], item)
                if current["id"] not in seen:
                    rebuilt.append(current)
                    seen.add(current["id"])
            self._items = rebuilt
        return self.read()

    def read(self) -> List[Dict[str, str]]:
        """Return a copy of the current list."""
        return [item.copy() for item in self._items]

    def has_items(self) -> bool:
        """Check if there are any items in the list."""
        return bool(self._items)

    def format_for_injection(self) -> Optional[str]:
        """Render the todo list for post-compression injection.

        Returns a human-readable string to append to the compressed
        message history, or None if the list is empty.
        """
        if not self._items:
            return None

        # Status markers for compact display
        markers = {
            "completed": "[x]",
            "in_progress": "[>]",
            "pending": "[ ]",
            "cancelled": "[~]",
        }

        # Only inject pending/in_progress items — completed/cancelled ones
        # cause the model to re-do finished work after compression.
        active_items = [
            item for item in self._items
            if item["status"] in ("pending", "in_progress")
        ]
        if not active_items:
            return None

        lines = ["[Your active task list was preserved across context compression]"]
        for item in active_items:
            marker = markers.get(item["status"], "[?]")
            lines.append(f"- {marker} {item['id']}. {item['content']} ({item['status']})")

        return "\n".join(lines)

    @staticmethod
    def _validate(item: Dict[str, Any]) -> Dict[str, str]:
        """Validate and normalize a todo item.

        Ensures required fields exist and status is valid.
        Returns a clean dict with only {id, content, status}.
        """
        item_id = str(item.get("id", "")).strip()
        if not item_id:
            item_id = "?"

        content = str(item.get("content", "")).strip()
        if not content:
            content = "(no description)"

        status = str(item.get("status", "pending")).strip().lower()
        if status not in VALID_STATUSES:
            status = "pending"

        return {"id": item_id, "content": content, "status": status}


# Per-session TodoStore instances, keyed by session_id string.
_session_stores: dict[str, TodoStore] = {}


def _get_store(session_id: Any) -> TodoStore:
    """Get or create a TodoStore for the given session.

    Thread-safe because each session runs in a single async task.
    """
    key = str(session_id) if session_id else "default"
    if key not in _session_stores:
        _session_stores[key] = TodoStore()
    return _session_stores[key]


def register(registry: ToolRegistry) -> None:
    """Register the todo tool."""
    registry.register(
        name="todo",
        schema=ToolSchema(
            name="todo",
            description=(
                "Manage your task list for the current session. Use for complex tasks "
                "with 3+ steps or when the user provides multiple tasks. "
                "Call with no parameters to read the current list.\n\n"
                "Writing:\n"
                "- Provide 'todos' array to create/update items\n"
                "- merge=false (default): replace the entire list with a fresh plan\n"
                "- merge=true: update existing items by id, add any new ones\n\n"
                "Each item: {id: string, content: string, "
                "status: pending|in_progress|completed|cancelled}\n"
                "List order is priority. Only ONE item in_progress at a time.\n"
                "Mark items completed immediately when done. If something fails, "
                "cancel it and add a revised item.\n\n"
                "Always returns the full current list."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": "Task items to write. Omit to read current list.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {
                                    "type": "string",
                                    "description": "Unique item identifier",
                                },
                                "content": {
                                    "type": "string",
                                    "description": "Task description",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed", "cancelled"],
                                    "description": "Current status",
                                },
                            },
                            "required": ["id", "content", "status"],
                        },
                    },
                    "merge": {
                        "type": "boolean",
                        "description": (
                            "true: update existing items by id, add new ones. "
                            "false (default): replace the entire list."
                        ),
                        "default": False,
                    },
                },
                "required": [],
            },
        ),
        handler=_todo_handler,
        toolset="todo",
    )


async def _todo_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Handle todo tool calls.

    Reads or writes depending on whether ``todos`` is provided.
    Uses a per-session TodoStore keyed by ``session_id`` from kwargs.
    """
    session_id = kwargs.get("session_id")
    store = _get_store(session_id)

    todos = arguments.get("todos")
    merge = arguments.get("merge", False)

    if todos is not None:
        items = store.write(todos, merge)
    else:
        items = store.read()

    # Build summary counts
    pending = sum(1 for i in items if i["status"] == "pending")
    in_progress = sum(1 for i in items if i["status"] == "in_progress")
    completed = sum(1 for i in items if i["status"] == "completed")
    cancelled = sum(1 for i in items if i["status"] == "cancelled")

    return json.dumps({
        "todos": items,
        "summary": {
            "total": len(items),
            "pending": pending,
            "in_progress": in_progress,
            "completed": completed,
            "cancelled": cancelled,
        },
    }, ensure_ascii=False)
