"""Projects, kept as ``workstreams``: what the master session's config says.

A project is a master web session, the coordinator, and the threads it
starts.  The server stamps three keys into their config, so the hot paths
(tool gates, the prompt, turn ends) decide without a join.  They are
server-owned: the session create route strips them from client config.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

#: Config keys only the server may write.
SERVER_OWNED_KEYS = ("workstream_id", "workstream_role", "workstream_tier")

#: ``workstream_role`` of a project's master session.
COORDINATOR = "coordinator"


def is_project_master(config: dict[str, Any] | None) -> bool:
    return (config or {}).get("workstream_role") == COORDINATOR


def master_refusal(command: str) -> str:
    """What a master answers to a command that would do its work in place."""
    return (
        f"/{command} does not run in a project's conversation. "
        "Ask for the work here, and it is given to a thread."
    )


def master_instructions(name: str, goal: str | None, instructions: str) -> str:
    """The master's session instructions: the project's name, goal and instructions."""
    return "\n\n".join(part for part in (f"Project: {name}", goal and f"Goal: {goal}", instructions) if part)


def master_config(
    workstream_id: UUID, *, name: str, goal: str | None, instructions: str,
) -> dict[str, Any]:
    """The config a project's master session is created with."""
    boundary = f"workstream:{workstream_id}"
    return {
        "coordinator": True,
        "strict_coordinator": True,
        "workstream_id": str(workstream_id),
        "workstream_role": COORDINATOR,
        "memory_boundary": boundary,
        "workspace_boundary": boundary,
        "system": master_instructions(name, goal, instructions),
    }
