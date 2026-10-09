"""Projects, kept as ``workstreams``: what the master session's config says.

A project is a master web session, the coordinator, and the threads it
starts.  The server stamps four keys into their config, so the hot paths
(tool gates, the prompt, turn ends) decide without a join.  They are
server-owned: the session create route strips them from client config.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from surogates.channels.memory_boundary import PROJECT_BOUNDARY_PREFIX

#: Config keys only the server may write.  ``workstream_card`` names the
#: proposal's card a thread on the user's computer begins with once bound.
SERVER_OWNED_KEYS = ("workstream_id", "workstream_role", "workstream_tier", "workstream_card")

#: ``workstream_role`` of a project's master session.
COORDINATOR = "coordinator"
#: ``workstream_role`` of a project's thread.
THREAD = "thread"


def is_project_master(config: dict[str, Any] | None) -> bool:
    return (config or {}).get("workstream_role") == COORDINATOR


def is_project_thread(config: dict[str, Any] | None) -> bool:
    return (config or {}).get("workstream_role") == THREAD


def thread_refusal(name: str) -> str:
    """What a project's thread answers to a command (``/name``) or tool *name*
    it cannot start yet.  A routine's runs would work on old files, and
    their work would land only when someone next speaks to the thread; a
    coding agent's turn ends outside the thread's landing, so its edits
    would stay in a copy never landed."""
    return f"A thread can't start {name} yet: do this step in the thread itself."


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
    boundary = f"{PROJECT_BOUNDARY_PREFIX}{workstream_id}"
    return {
        "coordinator": True,
        "strict_coordinator": True,
        "workstream_id": str(workstream_id),
        "workstream_role": COORDINATOR,
        "memory_boundary": boundary,
        "workspace_boundary": boundary,
        "system": master_instructions(name, goal, instructions),
    }


#: Names Windows keeps for its devices, with or without an extension.
_WINDOWS_DEVICE = re.compile(r"(con|prn|aux|nul|com[0-9]|lpt[0-9])(\.|$)", re.IGNORECASE)


def thread_folder(title: str) -> str:
    """Where a thread saves the files it makes: ``threads/<its title>/``, the
    title made a folder name on every system, a Windows laptop's included."""
    name = " ".join(re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', " ", title).split()).strip(". ")[:80].rstrip(". ")
    if _WINDOWS_DEVICE.match(name):
        name = f"thread {name}"
    return f"threads/{name or 'thread'}/"


def thread_config(project: Any, *, title: str) -> dict[str, Any]:
    """The project's keys a thread is created with.

    Built afresh, never copied from the master, so a thread carries neither
    the coordinator's role nor its strict mode.  Its session instructions
    are its title, its folder and a copy of the project's instructions: a
    later change reaches new threads, never one already started.
    """
    config: dict[str, Any] = {
        "workstream_id": str(project.id),
        "workstream_role": THREAD,
        "system": "\n\n".join(part for part in (
            # The name is folded onto its line, so no line of it reads as one
            # the server wrote; the title is one line already.
            f"Project: {' '.join(project.name.split())}\nThread: {title}\nFolder: {thread_folder(title)}",
            project.instructions,
        ) if part),
    }
    if project.thread_tier is not None:
        config["workstream_tier"] = project.thread_tier
    return config
