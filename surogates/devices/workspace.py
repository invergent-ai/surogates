"""A workspace folder on the user's computer, reached through device operations.

Every WorkspaceIO call becomes one operation: a kind and JSON arguments the
laptop runs against the session's bound folder, answered by an outcome,
``{"ok": <value>}`` or ``{"error": {...}}``.  The laptop resolves paths and
enforces its rules; the worker never touches the folder.

  kind            args                                          ok value
  bind            folder, nonce                                 null
  resolve         path                                          key (str)
  check_write     path                                          refusal (str) or null
  stat            key                                           {is_dir, size, mtime, revision} or null
  read            key, max_bytes (int or null)                  data (base64), or
                                                                {"transfer": {size, sha256}}
  read_lines      key, encoding, offset, limit, max_bytes       {data (base64), total_lines}
  write           key, data (base64), or                        null
                  key, transfer {size, sha256};
                  and expected_revision, when the call has one
  delete          key                                           null
  list_dir        key                                           [name]
  walk            key, skip ([name]), skip_top ([name]),        {files: [[path, size]], truncated, cursor}
                  skip_hidden (bool), since (up to 20 digits, or null)
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

A revision is "dev:ino:size:mtime_ns:ctime_ns", each the file's stat field in
decimal (dev and ino unsigned), as LocalWorkspaceIO.stat makes it.  The worker
never reads it: it compares it and hands it back as a write's
expected_revision.  A write that names one replaces the file only while it is
at that revision; otherwise, the file gone included, it is answered with a
conflict and nothing is written.  An expected_revision absent or null is no
expectation: the write replaces whatever is there.  A write over
MAX_WRITE_BYTES is answered with EFBIG before its revision is checked.

read_lines is a page of a text file, as WorkspaceIO.read_lines defines it
(surogates.tools.workspace_io.base): encoding is one of the six codecs
read_file picks, offset counts lines from 1, limit is any integer and selects
lines as a Python slice does, and max_bytes is at most MAX_PAYLOAD_BYTES.  The
computer only finds line ends; the worker decodes the page and applies every
rule.  Arguments it cannot take are answered with a value error.

walk lists the regular files under the folder at key, each as its path from
key ("sub/a.txt") and its size.  It follows no link: it enters each folder
through a handle on its parent, so a folder swapped for a link while it walks
is not entered.  It enters no folder whose name is in skip, none directly
under key whose name is in skip_top, and, with skip_hidden, none whose name
starts with "." other than SHOWN_DOT_FOLDERS: the file panel's tree shows none
of them.  A name that is not valid UTF-8 is left out, as are the files under
it.  With since, a cursor an earlier walk returned, it lists only the files
whose mtime or ctime is at or after it.  cursor is the computer's own clock as
this walk began, in nanoseconds, less WALK_MARGIN_NS: a filesystem
stamps changes by a coarser clock than the one the computer reads, a FAT
folder's in two-second ticks.  So a walk since it lists what changed after it
by that computer's clock, whatever the server's says.  It lists at most
MAX_WALK_FILES files, whose paths, each measured JSON-encoded plus 24, fit in
MAX_PAYLOAD_BYTES; it looks at most MAX_WALK_LOOKS entries, for at most
WALK_BUDGET_S seconds, since the folder's other operations wait behind it.
truncated says one of these caps stopped it.  A folder under key it cannot
read is left out; key itself unreadable is an os error.  Arguments it cannot
take are answered with a value error.

An error names the exception the worker raises again:

  {"type": "sandbox", "message"}          WorkspaceSandboxError: the path leaves the folder
  {"type": "os", "code", "message"}       OSError: code is the errno name ("ENOENT"),
                                          message is str(exc) without "[Errno N] "
  {"type": "ripgrep", "message"}          RipgrepError
  {"type": "value", "message"}            ValueError, e.g. a NUL byte in a path
  {"type": "conflict", "message"}         RevisionConflict: the file is not at the
                                          revision the write expected
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

  - a write carries at most MAX_PAYLOAD_BYTES of file data as its data.  Up to
    MAX_WRITE_BYTES the data is a transfer: in place of it the args name it by
    its size and the SHA-256 of the data, in lowercase hex, and the data
    follows the op in chunks (surogates.devices.link).  More is refused with
    EFBIG (and WRITE_TOO_LARGE) before anything is sent;
  - a read returns at most MAX_READ_BYTES, and fails with EFBIG (and
    READ_TOO_LARGE) when it would return more.  Data of up to
    MAX_PAYLOAD_BYTES is the ok value itself.  More is a transfer: the ok
    value names it by its size and the SHA-256 of the data, in lowercase hex,
    and the data follows in chunks (surogates.devices.link);
  - a read_lines page holds at most max_bytes, so it is always the ok value
    itself.  A file over MAX_READ_BYTES fails with EFBIG (and READ_TOO_LARGE)
    from its size, before it is scanned;
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

import asyncio
import base64
import contextlib
import errno
import hashlib
import json
import tempfile
from collections.abc import AsyncIterator, Collection
from pathlib import Path
from typing import Any, Protocol

from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError
from surogates.tools.workspace_io.base import (
    FileStat,
    LinePage,
    RevisionConflict,
    RipgrepError,
    RipgrepMode,
    RunResult,
    Walk,
)

MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_READ_BYTES = 50 * 1024 * 1024
MAX_WRITE_BYTES = 50 * 1024 * 1024
# A transfer's chunks carry this much of its data each, the last one the rest.
CHUNK_BYTES = MAX_PAYLOAD_BYTES
MAX_MESSAGE_CHARS = 1536 * 1024
OUTPUT_CAP_CHARS = 256 * 1024
MAX_NAMES = 10_000
MAX_WALK_FILES = 5_000
SHOWN_DOT_FOLDERS = (".github", ".vscode")
MAX_WALK_LOOKS = 200_000
WALK_MARGIN_NS = 2_000_000_000
WALK_BUDGET_S = 5
TOO_LARGE = "Too large for one operation on a local folder (over 1.5 MiB)"
READ_TOO_LARGE = "File too large to read from a local folder (over 50 MiB)"
WRITE_TOO_LARGE = "File too large to write to a local folder (over 50 MiB)"


class DeviceOperationError(RuntimeError):
    """The laptop failed an operation for a reason with no closer Python type."""


class OperationRunner(Protocol):
    async def run(self, kind: str, args: dict[str, Any], payload: bytes | None = None) -> dict[str, Any]:
        """Run one operation on the laptop and return its outcome.

        *payload* is a write's data that its args name as a transfer.
        """


def is_well_formed(value: Any) -> bool:
    """Whether *value*, a JSON value, encodes as UTF-8: it holds no lone UTF-16 surrogate."""
    try:
        json.dumps(value, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def transfer_of(outcome: dict[str, Any]) -> Any:
    """The transfer a read's outcome names in place of its data, or None."""
    ok = outcome.get("ok")
    return ok.get("transfer") if isinstance(ok, dict) else None


def _transfer_for(data: bytes) -> dict[str, Any]:
    """The transfer that carries *data*, named by its content."""
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


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
    if kind == "conflict":
        raise RevisionConflict(message)
    raise DeviceOperationError(message)


class DeviceWorkspaceIO:
    """WorkspaceIO for a session's folder on the user's computer."""

    def __init__(
        self, runner: OperationRunner, *, root: str, identity: str | None = None, caches_documents: bool = True,
    ) -> None:
        self._runner = runner
        self.root = root
        self.identity = identity
        self.caches_documents = caches_documents

    async def _call(self, kind: str, *, payload: bytes | None = None, **args: Any) -> Any:
        if len(json.dumps(args)) > MAX_MESSAGE_CHARS:
            raise OSError(errno.EFBIG, TOO_LARGE)
        outcome = await self._runner.run(kind, args, payload)
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
        if value is None:
            return None
        # Writes check it and the document cache keys by it: a stat without one is no stat.
        if not isinstance(value, dict) or not isinstance(value.get("revision"), str):
            raise DeviceOperationError("The computer returned an invalid stat")
        return FileStat(**value)

    async def read(self, key: str, max_bytes: int | None = None) -> bytes:
        data = await self._call("read", key=key, max_bytes=max_bytes)
        if isinstance(data, bytes):
            # A transfer's data, which the journal's runner fetched and checked.
            return data
        try:
            # Strict: a lenient decode drops what it does not know, such as the
            # "-" and "_" of base64url, and a patch would write the result back.
            return base64.b64decode(data, validate=True)
        except (ValueError, TypeError):
            raise DeviceOperationError("The computer returned invalid data") from None

    async def read_lines(
        self, key: str, *, encoding: str, offset: int, limit: int, max_bytes: int,
    ) -> LinePage:
        # At most one frame's data, so a page is always the ok value itself.
        asked = min(max_bytes, MAX_PAYLOAD_BYTES)
        value = await self._call(
            "read_lines", key=key, encoding=encoding,
            # Python's slice reads True as 1, as the cloud does; JSON would send true.
            offset=int(offset) if isinstance(offset, bool) else offset,
            limit=int(limit) if isinstance(limit, bool) else limit,
            max_bytes=asked,
        )
        try:
            if type(value["total_lines"]) is int and value["total_lines"] >= 0:
                # Strict, as a read's data is.
                data = base64.b64decode(value["data"], validate=True)
                # No correct computer sends more than it was asked for.
                if len(data) <= asked:
                    return LinePage(data, value["total_lines"])
        except (KeyError, TypeError, ValueError):
            pass
        raise DeviceOperationError("The computer returned an invalid page")

    async def write(self, key: str, data: bytes, *, expected_revision: str | None = None) -> None:
        # Only when there is one: a write that expects nothing asks for what it always did.
        expected = {} if expected_revision is None else {"expected_revision": expected_revision}
        if len(data) <= MAX_PAYLOAD_BYTES:
            await self._call("write", key=key, data=base64.b64encode(data).decode("ascii"), **expected)
            return
        if len(data) > MAX_WRITE_BYTES:
            raise OSError(errno.EFBIG, WRITE_TOO_LARGE)
        # Named by its content, so a resumed call that makes the same bytes asks
        # for the same operation.  Up to 50 MiB: hashed off the event loop the
        # worker's other sessions share.
        transfer = await asyncio.to_thread(_transfer_for, data)
        await self._call("write", payload=data, key=key, transfer=transfer, **expected)

    async def delete(self, key: str) -> None:
        await self._call("delete", key=key)

    async def list_dir(self, key: str) -> list[str]:
        return await self._call("list_dir", key=key)

    async def walk(
        self, key: str, *, skip: Collection[str], skip_top: Collection[str] = (), skip_hidden: bool = False,
        since: str | None = None,
    ) -> Walk:
        if isinstance(skip, str) or isinstance(skip_top, str):
            # A string is a collection of its characters: sorted, it would skip every one-letter folder.
            raise TypeError("skip and skip_top take folder names, not one string")
        value = await self._call(
            "walk", key=key, skip=sorted(skip), skip_top=sorted(skip_top), skip_hidden=skip_hidden, since=since,
        )
        try:
            files, truncated, cursor = value["files"], value["truncated"], value["cursor"]
            if (
                isinstance(files, list) and len(files) <= MAX_WALK_FILES
                and all(
                    isinstance(entry, list) and len(entry) == 2 and isinstance(entry[0], str)
                    and type(entry[1]) is int and entry[1] >= 0
                    for entry in files
                )
                and type(truncated) is bool
                # Taken here, not at the next walk's since: up to 20 digits, as the computer takes it back.
                and isinstance(cursor, str) and cursor.isascii() and cursor.isdigit() and len(cursor) <= 20
            ):
                return Walk([(path, size) for path, size in files], truncated, cursor)
        except (KeyError, TypeError):
            pass
        raise DeviceOperationError("The computer returned an invalid listing")

    @contextlib.asynccontextmanager
    async def local_file(self, key: str) -> AsyncIterator[Path]:
        data = await self.read(key)
        with tempfile.TemporaryDirectory(prefix="surogates-device-") as folder:
            path = Path(folder) / f"document{Path(key).suffix}"
            # Up to MAX_READ_BYTES: written off the loop, and not held again in
            # memory while the caller parses the file.
            await asyncio.to_thread(path.write_bytes, data)
            del data
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
