"""How tools reach a session's workspace.

The file tools, ``terminal``, the ``process`` tool and the research tools
act on the workspace only through a :class:`WorkspaceIO` taken from their
dispatch kwargs (see :func:`surogates.tools.workspace_io.workspace_io_from`).
The Python logic -- patch matching, read tracking, document parsing, output
limits, lint -- stays in the handlers.  An implementation supplies only raw
operations, so the same handlers can serve the cloud sandbox and a folder on
the user's laptop.

Keys: :meth:`WorkspaceIO.resolve` turns a path as the model wrote it into a
key, an absolute path in the workspace's own namespace.  Every other file
method takes a key.  Handlers may compare and show keys and take their
suffix or basename, but never hand one to this host's filesystem: for a
laptop workspace it names a file on another machine.

Implementations are request-scoped and must not change the process's
working directory or environment.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

RipgrepMode = Literal["files", "count", "json"]


@dataclass(frozen=True, slots=True)
class FileStat:
    """What a handler may know about a file without reading it."""

    is_dir: bool
    size: int
    mtime: float


@dataclass(frozen=True, slots=True)
class RunResult:
    """A finished command.  ``output`` is stdout, then stderr."""

    output: str
    returncode: int
    timed_out: bool = False


class RipgrepError(RuntimeError):
    """ripgrep is missing, or exited 2+ (exit 1 only means no matches)."""


class WorkspaceIO(Protocol):
    """Raw workspace operations.  Every rule about using them stays in the handlers."""

    root: str | None
    """The workspace this IO is bound to, spelled as a key; None when unbound."""

    # -- files -----------------------------------------------------------

    async def resolve(self, path: str) -> str:
        """Return the key for *path* as the model wrote it.

        Relative paths start at :attr:`root`.  Raises
        :class:`~surogates.tools.utils.workspace_sandbox.WorkspaceSandboxError`
        when *path* leaves the workspace.
        """

    async def check_write(self, path: str) -> str | None:
        """Why writing *path* is refused, worded for the model, or None."""

    async def stat(self, key: str) -> FileStat | None:
        """The file at *key*, or None when nothing is there."""

    async def read(self, key: str, max_bytes: int | None = None) -> bytes:
        """The file's bytes, or only its first *max_bytes*.  Raises OSError."""

    async def write(self, key: str, data: bytes) -> None:
        """Replace the file with *data* atomically, creating parent directories.

        An existing file keeps its permission bits.  Raises OSError.
        """

    async def delete(self, key: str) -> None:
        """Remove the file at *key*.  Raises OSError."""

    async def list_dir(self, key: str) -> list[str]:
        """Names in the directory at *key*.  Raises OSError."""

    def local_file(self, key: str) -> AbstractAsyncContextManager[Path]:
        """A path on this host holding the file's bytes, for parsers that need one."""

    async def ripgrep(
        self,
        key: str,
        *,
        mode: RipgrepMode,
        pattern: str,
        glob: str | None = None,
        context: int = 0,
    ) -> str:
        """Run ripgrep under *key*, where the files are, and return its stdout.

        ``files`` lists files matching the glob *pattern*.  ``count`` prints
        ``path:count`` per file for the regex *pattern*.  ``json`` streams
        ``rg --json`` events with *context* lines.  *glob* filters the files
        searched in ``count`` and ``json`` modes.  Raises :class:`RipgrepError`.
        """

    async def which(self, name: str) -> bool:
        """Whether the command *name* exists where commands run."""

    # -- commands and background processes -------------------------------

    async def run(self, command: str, *, workdir: str | None, timeout: int) -> RunResult:
        """Run a shell command in the workspace, sandboxed as terminal commands are.

        *workdir* is as the model wrote it; None means the root.  Raises
        WorkspaceSandboxError, worded for the model, when it is not allowed.
        """

    async def start(
        self,
        command: str,
        *,
        workdir: str | None,
        task_id: str,
        pty: bool,
        notify_on_complete: bool,
        watcher_interval: int | None,
    ) -> dict[str, Any]:
        """Start a background command and return ``{"session_id", "pid"}``.

        Raises WorkspaceSandboxError as :meth:`run` does.
        """

    # The process methods return the ``process`` tool's result shapes; see
    # :class:`surogates.tools.utils.process_registry.ProcessRegistry`.

    async def poll(self, session_id: str) -> dict[str, Any]:
        """Status and recent output of a background process."""

    async def read_output(self, session_id: str, *, offset: int, limit: int) -> dict[str, Any]:
        """A page of a background process's output lines."""

    async def wait(self, session_id: str, *, timeout: int | None) -> dict[str, Any]:
        """Wait until the process exits or *timeout* seconds pass."""

    async def kill(self, session_id: str) -> dict[str, Any]:
        """Terminate a background process."""

    async def write_stdin(self, session_id: str, data: str) -> dict[str, Any]:
        """Send *data* to the process's stdin, unchanged."""

    async def list_processes(self, task_id: str | None) -> list[dict[str, Any]]:
        """Summaries of running and recently finished processes."""
