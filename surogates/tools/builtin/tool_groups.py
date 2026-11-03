"""Builtin ``load_tools`` -- bring a held-back tool group into the session.

Offered only when the session's prompt profile declares tool groups (see
:mod:`surogates.harness.tool_groups`, which also rewrites this schema to list
the groups still held back). The harness adds a loaded group's schemas from
the model's next step; this handler only confirms the names and returns the
group's usage notes.
"""

from __future__ import annotations

import json
from typing import Any

from surogates.tools.registry import ToolRegistry, ToolSchema


async def _load_tools_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    from surogates.harness.tool_groups import all_tool_groups

    catalog = all_tool_groups()
    asked = arguments.get("groups") or []
    if isinstance(asked, str):
        asked = [asked]
    loaded = [g for g in asked if g in catalog]
    unknown = [g for g in asked if g not in catalog]
    if not loaded:
        return json.dumps({
            "success": False,
            "error": f"No such tool group: {', '.join(map(str, unknown)) or '(none given)'}.",
            "groups": sorted(catalog),
        })
    out: dict[str, Any] = {
        "success": True,
        "loaded": loaded,
        "message": "Loaded. Their tools are available from your next step.",
    }
    guides = {g: catalog[g].guide for g in loaded if catalog[g].guide}
    if guides:
        out["guide"] = guides
    if unknown:
        out["unknown"] = unknown
    return json.dumps(out, ensure_ascii=False)


def register(registry: ToolRegistry) -> None:
    """Register the load_tools tool."""
    registry.register(
        name="load_tools",
        schema=ToolSchema(
            name="load_tools",
            description=(
                "Load more tool groups into this session. The available groups "
                "are listed here when any are held back."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "groups": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Names of the groups to load.",
                    },
                },
                "required": ["groups"],
            },
        ),
        handler=_load_tools_handler,
        toolset="tool_groups",
    )
