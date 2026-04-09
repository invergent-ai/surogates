"""Builtin file operation tools -- file_read and file_write.

These tools are classified as sandbox tools: in production the
:class:`ToolRouter` forwards them to the sandbox runtime.  The handlers
here serve as harness-local fallbacks for development and testing.
"""

from __future__ import annotations

import json
from typing import Any

from surogates.tools.registry import ToolRegistry, ToolSchema


def register(registry: ToolRegistry) -> None:
    """Register file_read and file_write tools."""
    registry.register(
        name="file_read",
        schema=ToolSchema(
            name="file_read",
            description=(
                "Read the contents of a file from the sandbox workspace.  "
                "Returns the file content as a string."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Relative or absolute path to the file to read."
                        ),
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        ),
        handler=_file_read_handler,
        toolset="core",
    )

    registry.register(
        name="file_write",
        schema=ToolSchema(
            name="file_write",
            description=(
                "Write content to a file in the sandbox workspace.  "
                "Creates the file if it does not exist, overwrites if it does."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Relative or absolute path to the file to write."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "The content to write to the file.",
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        ),
        handler=_file_write_handler,
        toolset="core",
    )


async def _file_read_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Harness-local fallback: read a file from the local filesystem.

    In production, the ToolRouter sends file_read to the sandbox runtime
    instead of invoking this handler.
    """
    path = arguments.get("path", "")
    if not path:
        return json.dumps({"error": "No path provided"})

    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return json.dumps({"error": f"File not found: {path}"})
    except OSError as exc:
        return json.dumps({"error": f"Failed to read file: {exc}"})


async def _file_write_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Harness-local fallback: write a file to the local filesystem.

    In production, the ToolRouter sends file_write to the sandbox runtime
    instead of invoking this handler.
    """
    path = arguments.get("path", "")
    content = arguments.get("content", "")
    if not path:
        return json.dumps({"error": "No path provided"})

    try:
        import os

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return json.dumps({"status": "ok", "path": path, "bytes_written": len(content)})
    except OSError as exc:
        return json.dumps({"error": f"Failed to write file: {exc}"})
