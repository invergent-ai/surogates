"""Builtin web search tool -- placeholder until a search provider is configured."""

from __future__ import annotations

import json
from typing import Any

from surogates.tools.registry import ToolRegistry, ToolSchema


def register(registry: ToolRegistry) -> None:
    """Register the web_search tool."""
    registry.register(
        name="web_search",
        schema=ToolSchema(
            name="web_search",
            description=(
                "Search the web for information.  Returns relevant "
                "results as a JSON array."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        handler=_web_search_handler,
        toolset="web",
    )


async def _web_search_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Placeholder -- returns an informational message."""
    return json.dumps(
        {
            "error": "not_configured",
            "message": (
                "Web search is not yet configured for this deployment.  "
                "Contact the platform administrator to enable a search "
                "provider."
            ),
        }
    )
