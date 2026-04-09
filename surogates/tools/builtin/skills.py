"""Builtin skills listing tool.

Lists available skills from the :class:`ResourceLoader`, merging
platform, org, and user layers.
"""

from __future__ import annotations

import json
from typing import Any

from surogates.tools.registry import ToolRegistry, ToolSchema


def register(registry: ToolRegistry) -> None:
    """Register the skills_list tool."""
    registry.register(
        name="skills_list",
        schema=ToolSchema(
            name="skills_list",
            description=(
                "List all available skills for the current user.  Returns "
                "a JSON array of skill objects with name, description, and "
                "source (platform / org / user)."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        ),
        handler=_skills_list_handler,
        toolset="skills",
    )


async def _skills_list_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Return a JSON array of available skills."""
    tenant = kwargs.get("tenant")
    if tenant is None:
        return json.dumps({"error": "No tenant context available"})

    from surogates.tools.loader import ResourceLoader

    loader = ResourceLoader()
    skills = loader.load_skills(tenant)

    return json.dumps(
        [
            {
                "name": s.name,
                "description": s.description,
                "source": s.source,
            }
            for s in skills
        ],
        indent=2,
    )
