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
method takes a key, except :meth:`WorkspaceIO.check_write`, which takes the
path as written because handlers call it before ``resolve``.  Handlers may
compare and show keys and take their suffix or basename, but never hand one to
this host's filesystem: for a laptop workspace it names a file on another
machine.

Implementations are request-scoped and must not change the process's
working directory or environment.
"""

from __future__ import annotations

from collections.abc import Collection
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

# Where a NUL is refused, and in what words: said once, beside the sandbox's path rules.
from surogates.tools.utils.workspace_sandbox import NUL_REFUSED, refuse_nul

__all__ = ["NUL_REFUSED", "refuse_nul"]

RipgrepMode = Literal["files", "count", "json"]


@dataclass(frozen=True, slots=True)
class FileStat:
    """What a handler may know about a file without reading it.

    ``revision`` names this version of the file, opaquely, from its stat:
    device, inode, size, mtime and ctime.  Another file renamed onto its path
    and a change of size give another.  So does any other change to its
    content or metadata (an mtime put back, chmod, a new hard link, an xattr)
    made after the filesystem's timestamp tick.  A same-size change in place
    within one tick keeps it, and that tick can be seconds on FAT or HFS+; on
    vfat the ctime does not track changes.  A network folder whose attribute
    cache answers the stat can keep it for that cache's timeout.  Handlers
    only compare it and hand it back to :meth:`WorkspaceIO.write`.
    """

    is_dir: bool
    size: int
    mtime: float
    revision: str


@dataclass(frozen=True, slots=True)
class LinePage:
    """A page of a text file: its lines' bytes as they are, and how many lines the file has."""

    data: bytes
    total_lines: int


@dataclass(frozen=True, slots=True)
class Walk:
    """The regular files under a folder: each one's path from that folder, and its size.

    ``truncated`` says a cap stopped the walk before it saw everything.
    ``cursor`` is where a later walk's ``since`` starts, by the clock of the
    computer that holds the files; None where nothing keeps one.
    """

    files: list[tuple[str, int]]
    truncated: bool
    cursor: str | None


@dataclass(frozen=True, slots=True)
class RunResult:
    """A finished command.  ``output`` is stdout, then stderr."""

    output: str
    returncode: int
    timed_out: bool = False


class RipgrepError(RuntimeError):
    """ripgrep is missing, or exited 2+ with no output (exit 1 only means no matches)."""


class RevisionConflict(OSError):
    """The file is not at the revision a write expected, so it was not written.

    An OSError, so a handler that reports each file's failure reports this one too.
    """


class WorkspaceFiles(Protocol):
    """A session's files, as the api and the harness reach them outside a tool's handler.

    Files only: a workspace in object storage runs no command, so there is no
    process operation here, and nothing falls back to this host's shell.  Keys
    are :meth:`resolve`'s output, as for :class:`WorkspaceIO`, and never a path
    on this host.  ``surogates.session.files.session_files`` picks one.
    """

    identity: str | None
    """As :attr:`WorkspaceIO.identity`."""

    async def resolve(self, path: str) -> str:
        """The key for *path*, from the workspace's root.  Raises WorkspaceSandboxError when it leaves it."""

    async def stat(self, key: str) -> FileStat | None:
        """The file at *key*, or None when there is none."""

    async def read(self, key: str, max_bytes: int | None = None) -> bytes:
        """The file's bytes, or only its first *max_bytes*.  Raises FileNotFoundError, or another OSError."""

    async def write(self, key: str, data: bytes, *, expected_revision: str | None = None) -> None:
        """Replace the file with *data*, as :meth:`WorkspaceIO.write` does."""

    async def delete(self, key: str) -> None:
        """Remove the file at *key*.  Raises FileNotFoundError when there is none."""

    async def walk(
        self, key: str, *, skip: Collection[str], skip_top: Collection[str] = (), skip_hidden: bool = False,
    ) -> Walk:
        """The regular files under the folder at *key*.

        The caller does not want the files under a folder named in *skip*, one
        directly under *key* named in *skip_top*, or, with *skip_hidden*, a
        dot-folder other than ``SHOWN_DOT_FOLDERS``: a walk on a computer
        never enters them, which bounds its cost.  Object storage lists them
        all the same, in its one listing.
        """


class WorkspaceIO(Protocol):
    """Raw workspace operations.  Every rule about using them stays in the handlers."""

    root: str | None
    """The workspace as the dispatcher named it, or None when unbound.

    Not a key: it is not resolved, so it may differ from the prefix of keys.
    Handlers use it only to tell whether a workspace is bound, never to compare
    with or build keys; :meth:`resolve` does that.
    """

    identity: str | None
    """Whose files the keys name, for a cache that outlives the call: ``device:<id>``.

    A cache then keys a file by this, its key and its revision, and finds it
    before reading it.  None keys it by the file on this host, as the cloud
    does.
    """

    caches_documents: bool
    """Whether ``read_file`` may find or keep a document's parse in the cache.

    False for a call a worker resumes on a computer: a hit would skip a read
    its first run asked for, and that call would read as interrupted.
    """

    # -- files -----------------------------------------------------------

    async def resolve(self, path: str) -> str:
        """Return the key for *path* as the model wrote it.

        Relative paths start at :attr:`root`; an unbound IO resolves them from
        the process's working directory.  Raises
        :class:`~surogates.tools.utils.workspace_sandbox.WorkspaceSandboxError`
        when *path* leaves the workspace.
        """

    async def check_write(self, path: str) -> str | None:
        """Why writing *path* is refused, worded for the model, or None.

        Unlike the other file methods this takes *path* as the model wrote it,
        not a key: handlers call it before :meth:`resolve`.
        """

    async def stat(self, key: str) -> FileStat | None:
        """The file at *key*, or None when it cannot be stat'ed for any reason.

        Missing, permission denied, a symlink loop and an invalid path all give
        None; handlers treat None as "not found".
        """

    async def read(self, key: str, max_bytes: int | None = None) -> bytes:
        """The file's bytes, or only its first *max_bytes*.  Raises OSError."""

    async def read_lines(
        self, key: str, *, encoding: str, offset: int, limit: int, max_bytes: int,
    ) -> LinePage:
        """Lines *offset* (from 1) to *offset* + *limit* of the text file at *key*, as raw bytes.

        Its lines end as ``TextIOWrapper.readlines()`` ends them in *encoding*,
        one of :data:`surogates.tools.workspace_io.local.CODE_UNITS`: at a line
        feed, a carriage return, or both, as code units of the encoding.
        ``total_lines`` is how many lines ``readlines()`` gives.  ``data`` is
        the bytes of the lines ``lines[offset - 1:min(offset - 1 + limit,
        total_lines)]`` selects, a *limit* below one included, that fit whole
        in *max_bytes*.  When not even the first fits, it is that line's first
        *max_bytes* bytes.  A ``utf-8-sig`` file's BOM is in no line.  Decoded
        alone, in *encoding* (``utf-8`` for ``utf-8-sig``), ``data`` gives the
        same lines as the whole file does.  Raises OSError as :meth:`read` does.
        """

    async def write(self, key: str, data: bytes, *, expected_revision: str | None = None) -> None:
        """Replace the file with *data* atomically, creating parent directories.

        An existing file keeps its permission bits.  *expected_revision* is the
        :attr:`FileStat.revision` a stat in the same call gave: a workspace that
        checks it raises :class:`RevisionConflict` and writes nothing when the
        file is at another revision, or gone.  Raises OSError.
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
        searched in ``count`` and ``json`` modes.  Paths in the output are keys:
        handlers ``stat`` them and show them to the model.  Raises
        :class:`RipgrepError`.
        """

    async def which(self, name: str) -> bool:
        """Whether the command *name* exists where commands run."""

    # -- commands and background processes -------------------------------

    async def run(self, command: str, *, workdir: str | None, timeout: int) -> RunResult:
        """Run a shell command in the workspace, sandboxed as terminal commands are.

        *workdir* is as the model wrote it; None means the root.  Raises
        WorkspaceSandboxError, worded for the model, when it is not allowed.
        Any other exception means the command may have run; callers do not retry.
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
