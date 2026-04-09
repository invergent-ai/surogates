"""Builtin memory tools -- delegates to the MemoryManager.

Registers three tools:

- ``memory``       -- primary tool with add/replace/remove actions
- ``memory_read``  -- backward-compatible read (delegates to manager)
- ``memory_write`` -- backward-compatible append (delegates to manager)
"""

from __future__ import annotations

import json
from typing import Any

from surogates.tools.registry import ToolRegistry, ToolSchema


def register(registry: ToolRegistry) -> None:
    """Register memory, memory_read, and memory_write tools."""

    # -- Primary memory tool (action-based) ----------------------------------

    registry.register(
        name="memory",
        schema=ToolSchema(
            name="memory",
            description=(
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
                "additionalProperties": False,
            },
        ),
        handler=_memory_handler,
        toolset="memory",
    )

    # -- Backward-compatible tools ------------------------------------------

    registry.register(
        name="memory_read",
        schema=ToolSchema(
            name="memory_read",
            description=(
                "Read the contents of the persistent memory file for the "
                "current user.  Returns the full text of MEMORY.md."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        ),
        handler=_memory_read_handler,
        toolset="memory",
    )

    registry.register(
        name="memory_write",
        schema=ToolSchema(
            name="memory_write",
            description=(
                "Append content to the persistent memory file for the "
                "current user.  The content is appended as a new section "
                "to MEMORY.md."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": (
                            "The text to append to MEMORY.md.  Markdown "
                            "formatting is recommended."
                        ),
                    },
                },
                "required": ["content"],
                "additionalProperties": False,
            },
        ),
        handler=_memory_write_handler,
        toolset="memory",
    )


async def _memory_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Primary memory tool -- delegates to MemoryManager."""
    memory_manager = kwargs.get("memory_manager")
    if memory_manager is not None:
        return await memory_manager.handle_tool_call("memory", arguments)

    # Fallback: no manager available.
    return json.dumps({
        "success": False,
        "error": "Memory is not available. It may be disabled in config or this environment.",
    })


async def _memory_read_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Backward-compatible read -- delegates to MemoryManager or falls back to file I/O."""
    memory_manager = kwargs.get("memory_manager")
    if memory_manager is not None:
        # Read from the builtin provider's store.
        store = memory_manager._builtin.store
        entries = store.get_entries("memory")
        if not entries:
            return ""
        from surogates.memory.store import ENTRY_DELIMITER
        return ENTRY_DELIMITER.join(entries)

    # Fallback: direct file read (legacy behaviour).
    tenant = kwargs.get("tenant")
    if tenant is None:
        return json.dumps({"error": "No tenant context available"})

    from pathlib import Path
    memory_dir = Path(tenant.asset_root) / "memory"
    path = memory_dir / "MEMORY.md"
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        return json.dumps({"error": f"Failed to read memory file: {exc}"})


async def _memory_write_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Backward-compatible write -- delegates to MemoryManager or falls back to file I/O."""
    content = arguments.get("content", "")
    if not content:
        return json.dumps({"error": "No content provided"})

    memory_manager = kwargs.get("memory_manager")
    if memory_manager is not None:
        return await memory_manager.handle_tool_call("memory", {
            "action": "add",
            "target": "memory",
            "content": content,
        })

    # Fallback: direct file write (legacy behaviour).
    tenant = kwargs.get("tenant")
    if tenant is None:
        return json.dumps({"error": "No tenant context available"})

    import os
    from pathlib import Path
    path = Path(tenant.asset_root) / "memory" / "MEMORY.md"
    try:
        os.makedirs(path.parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            if path.is_file() and path.stat().st_size > 0:
                fh.write("\n\n")
            fh.write(content)
        return json.dumps({"status": "ok", "path": str(path)})
    except OSError as exc:
        return json.dumps({"error": f"Failed to write memory file: {exc}"})
