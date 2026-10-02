"""A workspace folder on the user's computer, reached through device operations.

Every WorkspaceIO call becomes one operation: a kind and JSON arguments the
laptop runs against the session's bound folder, answered by an outcome,
``{"ok": <value>}`` or ``{"error": {...}}``.  The laptop resolves paths and
enforces its rules; the worker never touches the folder.

  kind            args                                          ok value
  bind            folder, nonce                                 null
  resolve         path                                          key (str)
  check_write     path                                          refusal (str) or null
  stat            key                                           {is_dir, size, mtime} or null
  read            key, max_bytes (int or null)                  data (base64)
  write           key, data (base64)                            null
  delete          key                                           null
  list_dir        key                                           [name]
  ripgrep         key, mode, pattern, glob, context             stdout (str)
  which           name                                          bool
  run             command, workdir, timeout                     {output, returncode, timed_out}
  start           command, workdir, task_id, pty,
                  notify_on_complete, watcher_interval          {session_id, pid}
  poll            session_id                                    process status (object)
  read_output     session_id, offset, limit                     output page (object)
  wait            session_id, timeout                           process status (object)
  kill            session_id                                    process status (object)
  write_stdin     session_id, data                              status (object)
  list_processes  task_id                                       [process summary]

bind is a session's first operation, sent when the server creates a chat that
works on a folder of this computer.  The app answers it only after its user
confirmed that folder for this agent under the same nonce, and after it
recorded the binding; it refuses one it cannot match with
{"type": "binding", "message"}.  Every other operation is for a session the
app bound, or one created under it, and runs in the folder the app recorded,
whatever the request says.

An error names the exception the worker raises again:

  {"type": "sandbox", "message"}          WorkspaceSandboxError: the path leaves the folder
  {"type": "os", "code", "message"}       OSError: code is the errno name ("ENOENT"),
                                          message is str(exc) without "[Errno N] "
  {"type": "ripgrep", "message"}          RipgrepError
  {"type": "value", "message"}            ValueError, e.g. a NUL byte in a path
  any other type                          DeviceOperationError(message), including
                                          "revoked" (local access was revoked),
                                          "cancelled" (the session stopped it before
                                          the computer reported a result) and
                                          "too_large"

Data is standard base64 (RFC 4648 section 4: the "+" and "/" alphabet, padded
with "=", no line breaks).  A reply in any other form is refused, never decoded
loosely.

Sizes.  Every args object and every outcome, serialized, fits in
MAX_MESSAGE_CHARS, so it fits in one link frame:

  - a read or write carries at most MAX_PAYLOAD_BYTES of file data; a read
    that would return more fails with EFBIG (and TOO_LARGE) instead;
  - command output, and every string in a process outcome, keeps a head and a
    tail around a "chars omitted by the computer" marker, together at most
    OUTPUT_CAP_CHARS measured JSON-encoded (NUL and non-ASCII characters
    count as their \\uXXXX escapes, the most any JSON encoder writes);
  - ripgrep output over OUTPUT_CAP_CHARS, measured JSON-encoded, is answered with
    a ripgrep error asking the model to narrow the search, never cut, because
    the handlers count and page what they get and would report a partial result
    as complete;
  - list_dir returns at most MAX_NAMES names;
  - anything still too large is answered with {"type": "too_large"}.

Text.  Every string in an args object or an outcome is well-formed Unicode: a
lone UTF-16 surrogate cannot be sent as UTF-8, so the server refuses a request
that holds one and records a reply that holds one as an error.  (In Node,
String.prototype.toWellFormed() repairs a string.)
"""

from __future__ import annotations

import base64
import contextlib
import errno
import json
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol

from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError
from surogates.tools.workspace_io.base import FileStat, RipgrepError, RipgrepMode, RunResult

MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_MESSAGE_CHARS = 1536 * 1024
OUTPUT_CAP_CHARS = 256 * 1024
MAX_NAMES = 10_000
TOO_LARGE = "File too large for one operation on a local folder (over 1 MiB)"


class DeviceOperationError(RuntimeError):
    """The laptop failed an operation for a reason with no closer Python type."""


class OperationRunner(Protocol):
    async def run(self, kind: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run one operation on the laptop and return its outcome."""


def is_well_formed(value: Any) -> bool:
    """Whether *value*, a JSON value, encodes as UTF-8: it holds no lone UTF-16 surrogate."""
    try:
        json.dumps(value, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _raise(error: dict[str, Any]) -> None:
    kind = error.get("type")
    message = str(error.get("message", ""))
    if kind == "sandbox":
        raise WorkspaceSandboxError(message)
    if kind == "os":
        code = error.get("code")
        number = getattr(errno, code, None) if isinstance(code, str) else None
        if isinstance(number, int):
            raise OSError(number, message)
        raise OSError(message)
    if kind == "ripgrep":
        raise RipgrepError(message)
    if kind == "value":
        raise ValueError(message)
    raise DeviceOperationError(message)


class DeviceWorkspaceIO:
    """WorkspaceIO for a session's folder on the user's computer."""

    def __init__(self, runner: OperationRunner, *, root: str) -> None:
        self._runner = runner
        self.root = root

    async def _call(self, kind: str, **args: Any) -> Any:
        if len(json.dumps(args)) > MAX_MESSAGE_CHARS:
            raise OSError(errno.EFBIG, TOO_LARGE)
        outcome = await self._runner.run(kind, args)
        if "error" in outcome:
            _raise(outcome["error"])
        if "ok" not in outcome:
            raise DeviceOperationError(f"The computer returned no result for {kind}")
        return outcome["ok"]

    # -- files -----------------------------------------------------------

    async def resolve(self, path: str) -> str:
        return await self._call("resolve", path=path)

    async def check_write(self, path: str) -> str | None:
        return await self._call("check_write", path=path)

    async def stat(self, key: str) -> FileStat | None:
        value = await self._call("stat", key=key)
        return None if value is None else FileStat(**value)

    async def read(self, key: str, max_bytes: int | None = None) -> bytes:
        data = await self._call("read", key=key, max_bytes=max_bytes)
        try:
            # Strict: a lenient decode drops what it does not know, such as the
            # "-" and "_" of base64url, and a patch would write the result back.
            return base64.b64decode(data, validate=True)
        except (ValueError, TypeError):
            raise DeviceOperationError("The computer returned invalid data") from None

    async def write(self, key: str, data: bytes) -> None:
        if len(data) > MAX_PAYLOAD_BYTES:
            raise OSError(errno.EFBIG, TOO_LARGE)
        await self._call("write", key=key, data=base64.b64encode(data).decode("ascii"))

    async def delete(self, key: str) -> None:
        await self._call("delete", key=key)

    async def list_dir(self, key: str) -> list[str]:
        return await self._call("list_dir", key=key)

    @contextlib.asynccontextmanager
    async def local_file(self, key: str) -> AsyncIterator[Path]:
        data = await self.read(key)
        with tempfile.TemporaryDirectory(prefix="surogates-device-") as folder:
            path = Path(folder) / f"document{Path(key).suffix}"
            path.write_bytes(data)
            yield path

    async def ripgrep(
        self,
        key: str,
        *,
        mode: RipgrepMode,
        pattern: str,
        glob: str | None = None,
        context: int = 0,
    ) -> str:
        return await self._call(
            "ripgrep", key=key, mode=mode, pattern=pattern, glob=glob, context=context,
        )

    async def which(self, name: str) -> bool:
        return await self._call("which", name=name)

    # -- commands and background processes -------------------------------

    async def run(self, command: str, *, workdir: str | None, timeout: int) -> RunResult:
        value = await self._call("run", command=command, workdir=workdir, timeout=timeout)
        return RunResult(**value)

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
        return await self._call(
            "start",
            command=command,
            workdir=workdir,
            task_id=task_id,
            pty=pty,
            notify_on_complete=notify_on_complete,
            watcher_interval=watcher_interval,
        )

    async def poll(self, session_id: str) -> dict[str, Any]:
        return await self._call("poll", session_id=session_id)

    async def read_output(self, session_id: str, *, offset: int, limit: int) -> dict[str, Any]:
        return await self._call("read_output", session_id=session_id, offset=offset, limit=limit)

    async def wait(self, session_id: str, *, timeout: int | None) -> dict[str, Any]:
        return await self._call("wait", session_id=session_id, timeout=timeout)

    async def kill(self, session_id: str) -> dict[str, Any]:
        return await self._call("kill", session_id=session_id)

    async def write_stdin(self, session_id: str, data: str) -> dict[str, Any]:
        return await self._call("write_stdin", session_id=session_id, data=data)

    async def list_processes(self, task_id: str | None) -> list[dict[str, Any]]:
        return await self._call("list_processes", task_id=task_id)
