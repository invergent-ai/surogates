"""A laptop in a test: runs device operations the way Surogate Desktop does.

``perform`` is the laptop's half of the operation contract documented in
``surogates.devices.workspace``, including its size caps: it runs one
operation against a WorkspaceIO standing in for the bound folder and returns
the outcome the app would send.
"""

from __future__ import annotations

import asyncio
import base64
import errno
import json
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from surogates.devices.workspace import (
    MAX_MESSAGE_CHARS,
    MAX_NAMES,
    MAX_PAYLOAD_BYTES,
    OUTPUT_CAP_CHARS,
    TOO_LARGE,
)
from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError
from surogates.tools.workspace_io import RipgrepError, WorkspaceIO

_PROCESS_KINDS = {"start", "poll", "read_output", "wait", "kill", "write_stdin", "list_processes"}


async def perform(folder: WorkspaceIO, kind: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run one operation on *folder* and return its outcome."""
    try:
        outcome: dict[str, Any] = {"ok": await _run(folder, kind, args)}
    except WorkspaceSandboxError as exc:
        outcome = {"error": {"type": "sandbox", "message": str(exc)}}
    except RipgrepError as exc:
        outcome = {"error": {"type": "ripgrep", "message": str(exc)}}
    except OSError as exc:
        outcome = {"error": {
            "type": "os",
            "code": errno.errorcode.get(exc.errno) if exc.errno is not None else None,
            "message": str(exc).removeprefix(f"[Errno {exc.errno}] "),
        }}
    except ValueError as exc:
        outcome = {"error": {"type": "value", "message": str(exc)}}
    except Exception as exc:
        outcome = {"error": {"type": "other", "message": f"{type(exc).__name__}: {exc}"}}
    if len(json.dumps(outcome)) > MAX_MESSAGE_CHARS:
        outcome = {"error": {"type": "too_large", "message": f"The result of {kind} is too large"}}
    return outcome


def _encoded(text: str) -> int:
    # What the text costs on the wire.  ensure_ascii escapes NUL and every
    # non-ASCII character to \uXXXX, which no JSON encoder exceeds.
    return len(json.dumps(text))


def _cap_text(text: str) -> str:
    if _encoded(text) <= OUTPUT_CAP_CHARS:
        return text
    half = OUTPUT_CAP_CHARS // 2
    while half:
        size = _encoded(text[:half]) + _encoded(text[len(text) - half:])
        if size <= OUTPUT_CAP_CHARS:
            break
        half = min(half - 1, half * OUTPUT_CAP_CHARS // size)
    omitted = len(text) - 2 * half
    return f"{text[:half]}\n... [{omitted} chars omitted by the computer] ...\n{text[len(text) - half:]}"


def _cap_strings(value: Any) -> Any:
    if isinstance(value, str):
        return _cap_text(value)
    if isinstance(value, dict):
        return {key: _cap_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_cap_strings(item) for item in value]
    return value


async def _run(folder: WorkspaceIO, kind: str, a: dict[str, Any]) -> Any:
    if kind in _PROCESS_KINDS:
        return _cap_strings(await _run_process(folder, kind, a))
    if kind == "resolve":
        return await folder.resolve(a["path"])
    if kind == "check_write":
        return await folder.check_write(a["path"])
    if kind == "stat":
        st = await folder.stat(a["key"])
        return None if st is None else {"is_dir": st.is_dir, "size": st.size, "mtime": st.mtime}
    if kind == "read":
        wanted = a["max_bytes"]
        limit = MAX_PAYLOAD_BYTES + 1 if wanted is None else min(wanted, MAX_PAYLOAD_BYTES + 1)
        data = await folder.read(a["key"], limit)
        if len(data) > MAX_PAYLOAD_BYTES:
            raise OSError(errno.EFBIG, TOO_LARGE)
        return base64.b64encode(data).decode("ascii")
    if kind == "write":
        await folder.write(a["key"], base64.b64decode(a["data"]))
        return None
    if kind == "delete":
        await folder.delete(a["key"])
        return None
    if kind == "list_dir":
        return (await folder.list_dir(a["key"]))[:MAX_NAMES]
    if kind == "ripgrep":
        found = await folder.ripgrep(
            a["key"], mode=a["mode"], pattern=a["pattern"], glob=a["glob"], context=a["context"],
        )
        if _encoded(found) > OUTPUT_CAP_CHARS:
            # Cutting would hand the handlers a partial result that looks complete.
            raise RipgrepError(
                f"search output over {OUTPUT_CAP_CHARS} characters; narrow the pattern, path or glob"
            )
        return found
    if kind == "which":
        return await folder.which(a["name"])
    if kind == "run":
        result = await folder.run(a["command"], workdir=a["workdir"], timeout=a["timeout"])
        return {
            "output": _cap_text(result.output),
            "returncode": result.returncode,
            "timed_out": result.timed_out,
        }
    raise LookupError(f"unsupported operation {kind!r}")


async def _run_process(folder: WorkspaceIO, kind: str, a: dict[str, Any]) -> Any:
    if kind == "start":
        return await folder.start(
            a["command"],
            workdir=a["workdir"],
            task_id=a["task_id"],
            pty=a["pty"],
            notify_on_complete=a["notify_on_complete"],
            watcher_interval=a["watcher_interval"],
        )
    if kind == "poll":
        return await folder.poll(a["session_id"])
    if kind == "read_output":
        return await folder.read_output(a["session_id"], offset=a["offset"], limit=a["limit"])
    if kind == "wait":
        return await folder.wait(a["session_id"], timeout=a["timeout"])
    if kind == "kill":
        return await folder.kill(a["session_id"])
    if kind == "write_stdin":
        return await folder.write_stdin(a["session_id"], a["data"])
    return await folder.list_processes(a["task_id"])


class InProcessRunner:
    """Runs operations on a folder directly, through the JSON a link would carry."""

    def __init__(self, folder: WorkspaceIO) -> None:
        self.folder = folder
        self.kinds: list[str] = []

    async def run(self, kind: str, args: dict[str, Any]) -> dict[str, Any]:
        self.kinds.append(kind)
        outcome = await perform(self.folder, kind, json.loads(json.dumps(args)))
        wire = json.dumps(outcome)
        assert len(wire) <= MAX_MESSAGE_CHARS, f"{kind} outcome breaks the size contract"
        return json.loads(wire)


class FakeLaptop:
    """Surogate Desktop's side of the device link, in a test.

    Keeps each operation's outcome by id, like the app's journal, so an
    operation delivered again is answered without being run again.
    """

    def __init__(self, url: str, token: str, folder: WorkspaceIO, *, ping_interval_s: float = 0.2) -> None:
        self.url = url
        self.token = token
        self.folder = folder
        self.ping_interval_s = ping_interval_s
        self.ran: list[str] = []
        self.received: list[str] = []
        self.outcomes: dict[str, dict[str, Any]] = {}
        self.acked: set[str] = set()
        self.reply = True
        self.connected = False
        self._ws: ClientConnection | None = None
        self._tasks: list[asyncio.Task] = []

    async def connect(self) -> None:
        # The link's frames may be up to 2 MiB: one operation's 1 MiB of file
        # data, base64-encoded, plus its envelope.
        self._ws = await connect(
            self.url,
            additional_headers={"Authorization": f"Bearer {self.token}"},
            max_size=4 * 1024 * 1024,
        )
        await self._ws.send(json.dumps({"type": "hello", "protocols": [1]}))
        welcome = json.loads(await self._ws.recv())
        assert welcome["type"] == "welcome", welcome
        self.connected = True
        self._tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._ping())]

    async def disconnect(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._ws is not None:
            await self._ws.close()
        self.connected = False

    async def _ping(self) -> None:
        while True:
            await asyncio.sleep(self.ping_interval_s)
            await self._ws.send(json.dumps({"type": "ping"}))

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                frame = json.loads(raw)
                if frame["type"] == "op":
                    await self._handle(frame)
                elif frame["type"] == "op_ack":
                    self.acked.add(frame["id"])
        except ConnectionClosed:
            pass
        finally:
            self.connected = False

    async def _handle(self, frame: dict[str, Any]) -> None:
        operation_id = frame["id"]
        self.received.append(operation_id)
        if operation_id not in self.outcomes:
            self.ran.append(frame["kind"])
            self.outcomes[operation_id] = await perform(self.folder, frame["kind"], frame["args"])
        if not self.reply:
            await self._ws.close()
            return
        await self._ws.send(json.dumps({
            "type": "op_result",
            "id": operation_id,
            "digest": frame["digest"],
            "outcome": self.outcomes[operation_id],
        }))
