"""Builtin todo tool -- event-sourced task management per session.

Registers the ``todo`` tool with the tool registry.  Provides a
per-session task list for decomposing complex tasks, tracking progress,
and maintaining focus across long conversations.

The list is stored as ``todo.updated`` events, so it survives a wake
boundary: a new worker loads the latest snapshot rather than starting
blank.  Every response carries the full list, so recovery reads one row.

Design:
- Single ``todo`` tool: provide ``todos`` param to write, omit to read
- Every call returns the full current list
- No system prompt mutation, no tool response modification
- Behavioral guidance lives entirely in the tool schema description
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from surogates.session.events import EventType
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)

# Valid status values for todo items
VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}


class TodoStore:
    """Todo list. Built per call from the latest snapshot.

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

    The list lives in the event log, not in this process: it is loaded from
    the newest ``todo.updated`` snapshot on every call and a write appends a
    new one.  That is what keeps a merge correct across a wake boundary --
    the previous design kept the store in a module-global that a new worker
    started blank, so ``merge=true`` merged onto nothing and dropped the plan.
    """
    session_id = kwargs.get("session_id")
    session_store = kwargs.get("session_store")

    todos = arguments.get("todos")
    merge = arguments.get("merge", False)

    store = TodoStore()
    if session_store is not None and session_id:
        try:
            snapshot = await session_store.latest_todo_snapshot(session_id)
        except Exception:
            # Degrade to a blank list rather than failing the turn: a read
            # blip must not cost the model its tool call.
            logger.warning(
                "todo: could not load the plan for session %s",
                session_id, exc_info=True,
            )
            snapshot = None
        if snapshot:
            store.write(snapshot, merge=False)

    if todos is not None:
        items = store.write(todos, merge)
        if session_store is not None and session_id:
            # Deliberately not swallowed: if the plan cannot be persisted the
            # model must see the failure, or it proceeds against a list that
            # will be gone on the next wake.
            await session_store.emit_event(
                session_id, EventType.TODO_UPDATED, {"todos": items},
            )
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
