"""Builtin browser tool -- placeholder for sandbox-based browser automation.

This tool is classified as a sandbox tool.  The actual implementation
runs inside the sandbox runtime; this module registers the schema so
that the harness can advertise it to the LLM.
"""

from __future__ import annotations

import json
from typing import Any

from surogates.tools.registry import ToolRegistry, ToolSchema


def register(registry: ToolRegistry) -> None:
    """Register the browser_navigate tool."""
    registry.register(
        name="browser_navigate",
        schema=ToolSchema(
            name="browser_navigate",
            description=(
                "Navigate a headless browser to a URL and return the "
                "page content.  Runs inside the sandbox."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to navigate to.",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["get_text", "get_html", "screenshot"],
                        "description": (
                            "What to return: extracted text, raw HTML, "
                            "or a base64-encoded screenshot."
                        ),
                        "default": "get_text",
                    },
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        ),
        handler=_browser_navigate_handler,
        toolset="web",
    )


async def _browser_navigate_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Harness-local fallback -- browser tools require a sandbox."""
    return json.dumps(
        {
            "error": "sandbox_required",
            "message": (
                "browser_navigate requires execution inside a sandbox.  "
                "This harness-local fallback cannot perform browser "
                "automation."
            ),
        }
    )
