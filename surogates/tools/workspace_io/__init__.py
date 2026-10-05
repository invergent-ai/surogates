"""Workspace access for tools.  See :mod:`surogates.tools.workspace_io.base`."""

from __future__ import annotations

from typing import Any

from surogates.devices.binding import device_of, device_owners
from surogates.tools.workspace_io.base import (
    FileStat,
    LinePage,
    RevisionConflict,
    RipgrepError,
    RunResult,
    WorkspaceIO,
)
from surogates.tools.workspace_io.local import LocalWorkspaceIO

__all__ = [
    "FileStat",
    "LinePage",
    "LocalWorkspaceIO",
    "RevisionConflict",
    "RipgrepError",
    "RunResult",
    "WorkspaceIO",
    "workspace_io_from",
]


def workspace_io_from(kwargs: dict[str, Any]) -> WorkspaceIO:
    """The WorkspaceIO a handler was dispatched with.

    A dispatcher passes ``workspace_io`` to point the tools at a workspace
    elsewhere.  Without one the handler acts on ``workspace_path`` on this
    host, which is what every dispatcher does today.
    """
    wio = kwargs.get("workspace_io")
    if wio is not None:
        return wio
    if device_owners.get() or device_of(kwargs.get("session_config")) is not None:
        # The folder is on the user's computer: reaching it from this host
        # would read or write whatever this host has at that path.  The mark
        # covers callers that pass no session config (the expert loop).
        raise RuntimeError(
            "This session's folder is on the user's computer and is reached only through it"
        )
    return LocalWorkspaceIO(workspace_path=kwargs.get("workspace_path"))
