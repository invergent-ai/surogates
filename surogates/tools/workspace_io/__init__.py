"""Workspace access for tools.  See :mod:`surogates.tools.workspace_io.base`."""

from __future__ import annotations

from typing import Any

from surogates.tools.workspace_io.base import FileStat, RipgrepError, RunResult, WorkspaceIO
from surogates.tools.workspace_io.local import LocalWorkspaceIO

__all__ = [
    "FileStat",
    "LocalWorkspaceIO",
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
    return LocalWorkspaceIO(workspace_path=kwargs.get("workspace_path"))
