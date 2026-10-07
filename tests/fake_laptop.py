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
import hashlib
import json
import os
import re
import time
from collections.abc import Iterator
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from surogates.devices.link import TRANSFER_WINDOW
from surogates.devices.workspace import (
    CHUNK_BYTES,
    MAX_MESSAGE_CHARS,
    MAX_NAMES,
    MAX_PAYLOAD_BYTES,
    MAX_READ_BYTES,
    MAX_WALK_FILES,
    MAX_WALK_LOOKS,
    MAX_WRITE_BYTES,
    OUTPUT_CAP_CHARS,
    READ_TOO_LARGE,
    SHOWN_DOT_FOLDERS,
    WALK_BUDGET_S,
    WALK_MARGIN_NS,
    WRITE_TOO_LARGE,
    is_well_formed,
    transfer_of,
)
from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError
from surogates.tools.workspace_io import RevisionConflict, RipgrepError, WorkspaceIO
from surogates.tools.workspace_io.local import CODE_UNITS

_PROCESS_KINDS = {"start", "poll", "read_output", "wait", "kill", "write_stdin", "list_processes"}

# What a write whose data does not come whole and matching is answered, as the app answers it.
DAMAGED = {"error": {
    "type": "other",
    "message": "The data this computer received for this write was incomplete or did not match, so it was not written",
}}
# What a write whose args name its data in a form the app does not take is answered, as the app answers it.
MALFORMED_TRANSFER = {"error": {
    "type": "other",
    "message": "This write named its data in a form this computer does not take, so it was not written",
}}
_SHA256 = re.compile(r"[0-9a-f]{64}")
# What a write is answered when its file is not at the revision it expects, as the app answers it; {} is the key.
CONFLICT = (
    "{} changed on this computer after it was read, so it was not written. "
    "Read it again, then make the change again"
)
# What a page whose args the app does not take is answered, as the app answers it.
BAD_PAGE = (
    "read_lines takes one of the encodings read_file picks, an offset from 1, an integer limit "
    f"and a max_bytes from 0 to {MAX_PAYLOAD_BYTES}"
)
# What a walk whose args the app does not take is answered, as the app answers it.
BAD_WALK = (
    "walk takes a key, the folder names it skips anywhere and directly under the key, whether it skips "
    "hidden folders, and a since an earlier walk gave, or null"
)


def _malformed_write(args: dict[str, Any]) -> bool:
    """As the app's sentTransfer: more than MAX_PAYLOAD_BYTES inline, or a transfer named any other way."""
    # A key that is present counts, null or not, as the app's undefined checks count it.
    if "transfer" not in args:
        data = args.get("data")
        # As Buffer.byteLength(data, "base64") counts it.
        return isinstance(data, str) and len(data) * 3 // 4 - data[-2:].count("=") > MAX_PAYLOAD_BYTES
    transfer = args["transfer"]
    return not (
        isinstance(transfer, dict) and transfer.keys() == {"size", "sha256"} and "data" not in args
        and type(transfer["size"]) is int and MAX_PAYLOAD_BYTES < transfer["size"] <= MAX_WRITE_BYTES
        and isinstance(transfer["sha256"], str) and _SHA256.fullmatch(transfer["sha256"]) is not None
    )


def _integer(value: Any) -> bool:
    """As Number.isInteger sees what JSON.parse made of *value*.

    A bool is no integer, as Number.isInteger(true) is false, and one too
    large for a double is read as Infinity or -Infinity, which are none.
    """
    if type(value) is not int:
        return False
    try:
        float(value)
    except OverflowError:
        return False
    return True


def _page_args(args: dict[str, Any]) -> bool:
    """As the app checks a page's args."""
    encoding, offset, limit, max_bytes = (args.get(name) for name in ("encoding", "offset", "limit", "max_bytes"))
    return (
        isinstance(encoding, str) and encoding in CODE_UNITS
        and _integer(offset) and offset >= 1 and _integer(limit)
        and type(max_bytes) is int and 0 <= max_bytes <= MAX_PAYLOAD_BYTES
    )


async def perform(folder: WorkspaceIO, kind: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run one operation on *folder* and return its outcome."""
    try:
        outcome: dict[str, Any] = {"ok": await _run(folder, kind, args)}
    except WorkspaceSandboxError as exc:
        outcome = {"error": {"type": "sandbox", "message": str(exc)}}
    except RevisionConflict as exc:
        outcome = {"error": {"type": "conflict", "message": str(exc)}}
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
    # A read is bounded by MAX_READ_BYTES instead: its data over MAX_PAYLOAD_BYTES goes as a transfer.
    if kind != "read" and len(json.dumps(outcome)) > MAX_MESSAGE_CHARS:
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
        if st is None:
            return None
        return {"is_dir": st.is_dir, "size": st.size, "mtime": st.mtime, "revision": st.revision}
    if kind == "read":
        wanted = a["max_bytes"]
        limit = MAX_READ_BYTES + 1 if wanted is None else min(wanted, MAX_READ_BYTES + 1)
        data = await folder.read(a["key"], limit)
        if len(data) > MAX_READ_BYTES:
            raise OSError(errno.EFBIG, READ_TOO_LARGE)
        return base64.b64encode(data).decode("ascii")
    if kind == "read_lines":
        if not _page_args(a):
            raise ValueError(BAD_PAGE)
        # As the app: refused from the file's size, before it is scanned.
        st = await folder.stat(a["key"])
        if st is not None and st.size > MAX_READ_BYTES:
            raise OSError(errno.EFBIG, READ_TOO_LARGE)
        page = await folder.read_lines(
            a["key"], encoding=a["encoding"], offset=a["offset"], limit=a["limit"], max_bytes=a["max_bytes"],
        )
        return {"data": base64.b64encode(page.data).decode("ascii"), "total_lines": page.total_lines}
    if kind == "write":
        data = base64.b64decode(a["data"])
        if len(data) > MAX_WRITE_BYTES:
            raise OSError(errno.EFBIG, WRITE_TOO_LARGE)
        expected = a.get("expected_revision")
        # Checked here, as the app checks it: LocalWorkspaceIO writes whatever is there.  Nothing between this
        # check and folder.write may await: LocalWorkspaceIO's stat and write never suspend, so the two are one
        # step on the event loop, as the app's synchronous helper never yields between them.
        if expected is not None:
            st = await folder.stat(a["key"])
            if st is None or st.revision != expected:
                raise RevisionConflict(CONFLICT.format(a["key"]))
        await folder.write(a["key"], data)
        return None
    if kind == "delete":
        await folder.delete(a["key"])
        return None
    if kind == "list_dir":
        return (await folder.list_dir(a["key"]))[:MAX_NAMES]
    if kind == "walk":
        return _walk(a)
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


def _walk(a: dict[str, Any]) -> dict[str, Any]:
    """As the app walks: depth first, each folder entered as it is met, through a handle on its parent and never
    through a link, and each folder's entries read as the walk goes, as the operating system lists them."""
    key, skip, top, hidden, since = (a.get(name) for name in ("key", "skip", "skip_top", "skip_hidden", "since"))
    if (
        not isinstance(key, str) or type(hidden) is not bool
        or not all(isinstance(names, list) and all(isinstance(name, str) for name in names) for names in (skip, top))
        or not (since is None or (isinstance(since, str) and since.isascii() and since.isdigit()))
    ):
        raise ValueError(BAD_WALK)
    cursor = str(time.time_ns() - WALK_MARGIN_NS)
    deadline = time.monotonic() + WALK_BUDGET_S
    after = None if since is None else int(since)
    files: list[list[Any]] = []
    cost, looks, truncated = 2, 0, False
    # Each folder the walk is in: its handle, its path from the key, and its entries still to look at.
    levels: list[tuple[int, str, Iterator[os.DirEntry[str]]]] = []
    fd = os.open(key, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # Its entries' stats are taken through the handle too, so by no path that could hold a link.
        levels.append((fd, "", os.scandir(fd)))
    except OSError:
        os.close(fd)
        raise
    try:
        while levels and not truncated:
            fd, rel, entries = levels[-1]
            try:
                entry = next(entries, None)
            except OSError:
                entry = None  # what it could not read of a folder is left out
            if entry is None:
                levels.pop()
                _close(fd, entries)
                continue
            looks += 1
            if looks > MAX_WALK_LOOKS or time.monotonic() > deadline:
                truncated = True
                break
            path = f"{rel}/{entry.name}" if rel else entry.name
            if not is_well_formed(path):
                # As the app: a name that is not UTF-8 cannot be looked up by its decoded text.
                continue
            if entry.is_dir(follow_symlinks=False):
                hides = hidden and entry.name.startswith(".") and entry.name not in SHOWN_DOT_FOLDERS
                if entry.name in skip or (not rel and entry.name in top) or hides:
                    continue
                try:
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except OSError:
                    continue
                try:
                    levels.append((child, path, os.scandir(child)))
                except OSError:
                    os.close(child)
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if after is not None and st.st_mtime_ns < after and st.st_ctime_ns < after:
                continue
            more = len(json.dumps(path)) + 24
            if len(files) == MAX_WALK_FILES or cost + more > MAX_PAYLOAD_BYTES:
                truncated = True
                break
            files.append([path, st.st_size])
            cost += more
    finally:
        for fd, _, entries in levels:
            _close(fd, entries)
    return {"files": files, "truncated": truncated, "cursor": cursor}


def _close(fd: int, entries: Iterator[os.DirEntry[str]]) -> None:
    entries.close()  # type: ignore[attr-defined]  # a scandir iterator
    os.close(fd)


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

    async def run(self, kind: str, args: dict[str, Any], payload: bytes | None = None) -> dict[str, Any]:
        self.kinds.append(kind)
        sent = json.loads(json.dumps(args))
        if payload is not None:
            # A write's data that crossed as a transfer: whole, as its args name it, then written as one that carried it.
            named = sent.pop("transfer")
            assert named == {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
            sent["data"] = base64.b64encode(payload).decode("ascii")
        outcome = await perform(self.folder, kind, sent)
        wire = json.dumps(outcome)
        # A read's data over MAX_PAYLOAD_BYTES crosses a link as a transfer, outside the outcome.
        if kind != "read":
            assert len(wire) <= MAX_MESSAGE_CHARS, f"{kind} outcome breaks the size contract"
        return json.loads(wire)


class FakeLaptop:
    """Surogate Desktop's side of the device link, in a test.

    Keeps each operation's outcome by id, like the app's journal, so an
    operation delivered again is answered without being run again.  It reports
    the operations it holds unfinished in its hello, and never runs one it was
    told to cancel.  A read's data over MAX_PAYLOAD_BYTES goes as a transfer,
    one at a time, TRANSFER_WINDOW chunks ahead of the acknowledgements, and
    from chunk 0 again whenever the operation is delivered again, until the
    server acknowledges it or does not want it.  A write whose data comes as a
    transfer runs once that data is whole; every chunk is acknowledged, and a
    new connection starts each one over.
    """

    def __init__(self, url: str, token: str, folder: WorkspaceIO, *, ping_interval_s: float = 0.2) -> None:
        self.url = url
        self.token = token
        self.folder = folder
        self.ping_interval_s = ping_interval_s
        self.ran: list[str] = []
        self.received: list[str] = []
        self.outcomes: dict[str, dict[str, Any]] = {}
        # The data of each read answered with a transfer, and every chunk sent, as (id, seq).
        self.payloads: dict[str, bytes] = {}
        self.chunks_sent: list[tuple[str, int]] = []
        # Every chunk of a write's data received, as (id, seq).
        self.chunks_received: list[tuple[str, int]] = []
        # What the user confirmed with prepareFolder: nonce -> folder.
        self.prepared: dict[str, str] = {}
        # Root session id -> the folder the app bound it to.
        self.bindings: dict[str, str] = {}
        self.acked: set[str] = set()
        self.reply = True
        # Received operations are neither run nor answered: a long command.
        self.hold = False
        self.cancelled: set[str] = set()
        # The type of every frame received after welcome, in order.
        self.frames: list[str] = []
        self.connected = False
        self._ws: ClientConnection | None = None
        self._tasks: list[asyncio.Task] = []
        self._sending = asyncio.Lock()
        # On this connection: chunks acknowledged per transfer, and transfers that ended.
        self._chunk_acks: dict[str, int] = {}
        self._ended: set[str] = set()
        self._heard = asyncio.Event()
        # On this connection: each write waiting for its data, and what came of it.
        self._incoming: dict[str, tuple[dict[str, Any], bytearray]] = {}

    async def connect(self) -> None:
        # The link's frames may be up to 2 MiB: one operation's 1 MiB of file
        # data, base64-encoded, plus its envelope.
        ws = await connect(
            self.url,
            additional_headers={"Authorization": f"Bearer {self.token}"},
            max_size=4 * 1024 * 1024,
        )
        self._ws = ws
        unfinished = [i for i in self.received if i not in self.outcomes and i not in self.cancelled]
        await ws.send(json.dumps({"type": "hello", "protocols": [1], "open": unfinished}))
        welcome = json.loads(await ws.recv())
        assert welcome["type"] == "welcome", welcome
        self.connected = True
        self._chunk_acks, self._ended, self._incoming = {}, set(), {}
        # Each task works on its own connection's socket. Added to, not replaced: an earlier
        # connection's tasks still end at disconnect().
        self._tasks += [asyncio.create_task(self._read(ws)), asyncio.create_task(self._ping(ws))]

    def prepare(self, nonce: str, folder: str) -> None:
        """Stand in for the user confirming *folder* for a new chat."""
        self.prepared[nonce] = folder

    async def disconnect(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self._ws is not None:
            await self._ws.close()
        self.connected = False

    async def _ping(self, ws: ClientConnection) -> None:
        while True:
            await asyncio.sleep(self.ping_interval_s)
            await ws.send(json.dumps({"type": "ping"}))

    async def _read(self, ws: ClientConnection) -> None:
        try:
            async for raw in ws:
                if self._ws is not ws:
                    # A newer connection replaced this one: what it says is not for the laptop now.
                    break
                frame = json.loads(raw)
                self.frames.append(frame["type"])
                if frame["type"] == "op":
                    await self._handle(frame, ws)
                elif frame["type"] == "op_ack":
                    self.acked.add(frame["id"])
                    self._end(frame["id"])
                elif frame["type"] == "unwanted":
                    self._end(frame["id"])
                elif frame["type"] == "chunk_ack":
                    self._chunk_acks[frame["id"]] = frame["seq"] + 1
                elif frame["type"] == "chunk":
                    await self._chunk(frame, ws)
                elif frame["type"] == "cancel":
                    self.cancelled.add(frame["id"])
                self._heard.set()
        except ConnectionClosed:
            pass
        finally:
            if self._ws is ws:
                self.connected = False
            self._heard.set()

    def _bind(self, frame: dict[str, Any]) -> dict[str, Any]:
        args = frame["args"]
        # One use, like the app's preparation token.
        folder = self.prepared.pop(args["nonce"], None)
        if folder is None or folder != args["folder"]:
            return {"error": {"type": "binding", "message": "This folder was not confirmed on this computer"}}
        self.bindings[frame["session_id"]] = folder
        return {"ok": None}

    async def _handle(self, frame: dict[str, Any], ws: ClientConnection) -> None:
        operation_id = frame["id"]
        self.received.append(operation_id)
        if operation_id in self.cancelled or (self.hold and operation_id not in self.outcomes):
            # A cancelled operation is never run; a held one is still running.
            return
        if operation_id not in self.outcomes and frame["kind"] == "write":
            if _malformed_write(frame["args"]):
                # Answered, never run.
                self.outcomes[operation_id] = MALFORMED_TRANSFER
            elif "transfer" in frame["args"]:
                # A write whose data follows: it runs once that data is whole.
                self._incoming[operation_id] = (frame, bytearray())
                return
        await self._answer(frame, ws)

    async def _chunk(self, frame: dict[str, Any], ws: ClientConnection) -> None:
        """A chunk of a write's data: acknowledged, wanted or not, and checked, as the app does."""
        self.chunks_received.append((frame["id"], frame["seq"]))
        # This connection's: a newer one, made while this waits to send the ack, has its own.
        incoming = self._incoming
        await ws.send(json.dumps({"type": "chunk_ack", "id": frame["id"], "seq": frame["seq"]}))
        if frame["id"] in self.cancelled:
            # Stopped while its data came: it never runs.
            incoming.pop(frame["id"], None)
        if frame["id"] not in incoming:
            return
        op, data = incoming[frame["id"]]
        transfer = op["args"]["transfer"]
        piece = base64.b64decode(frame["data"], validate=True)
        in_place = (
            frame["seq"] == len(data) // CHUNK_BYTES
            and len(piece) == min(CHUNK_BYTES, transfer["size"] - len(data))
        )
        if in_place:
            data += piece
            if len(data) < transfer["size"]:
                return
        del incoming[frame["id"]]
        if not in_place or hashlib.sha256(data).hexdigest() != transfer["sha256"]:
            # Answered, never run.
            self.outcomes[frame["id"]] = DAMAGED
        # As the app's runner puts the data back inline: the other args, an expected revision too, stay.
        args = {name: value for name, value in op["args"].items() if name != "transfer"}
        args["data"] = base64.b64encode(data).decode("ascii")
        await self._answer({**op, "args": args}, ws)

    async def _answer(self, frame: dict[str, Any], ws: ClientConnection) -> None:
        operation_id = frame["id"]
        if operation_id not in self.outcomes:
            self.ran.append(frame["kind"])
            outcome = (
                self._bind(frame) if frame["kind"] == "bind"
                else await perform(self.folder, frame["kind"], frame["args"])
            )
            self.outcomes[operation_id] = self._carried(operation_id, frame["kind"], outcome)
        if not self.reply:
            await ws.close()
            return
        result = {
            "type": "op_result",
            "id": operation_id,
            "digest": frame["digest"],
            "outcome": self.outcomes[operation_id],
        }
        if operation_id in self.payloads:
            # From a task of its own: this reader goes on to read the acknowledgements.
            self._tasks.append(asyncio.create_task(self._transfer(result, self.payloads[operation_id], ws)))
        elif transfer_of(result["outcome"]) is None:
            await ws.send(json.dumps(result))
        # A transfer whose data is gone was acknowledged: the server has its result.

    def _end(self, operation_id: str) -> None:
        """The server recorded this result, or does not want it: its data is not sent again."""
        self._ended.add(operation_id)
        self.payloads.pop(operation_id, None)

    def _carried(self, operation_id: str, kind: str, outcome: dict[str, Any]) -> dict[str, Any]:
        """A read's data over MAX_PAYLOAD_BYTES leaves its outcome, which names it by size and SHA-256."""
        encoded = outcome.get("ok") if kind == "read" else None
        if not isinstance(encoded, str):
            return outcome
        data = base64.b64decode(encoded)
        if len(data) <= MAX_PAYLOAD_BYTES:
            return outcome
        self.payloads[operation_id] = data
        return {"ok": {"transfer": {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}}}

    async def _transfer(self, result: dict[str, Any], data: bytes, ws: ClientConnection) -> None:
        operation_id = result["id"]
        async with self._sending:
            if not self._going(operation_id, ws):
                return
            self._chunk_acks[operation_id] = 0
            try:
                await ws.send(json.dumps(result))
                for seq in range(-(-len(data) // CHUNK_BYTES)):
                    while self._going(operation_id, ws) and seq - self._chunk_acks[operation_id] >= TRANSFER_WINDOW:
                        await self._wait_to_hear()
                    if not self._going(operation_id, ws):
                        return
                    piece = data[seq * CHUNK_BYTES:(seq + 1) * CHUNK_BYTES]
                    await ws.send(json.dumps({
                        "type": "chunk", "id": operation_id, "seq": seq,
                        "data": base64.b64encode(piece).decode("ascii"),
                    }))
                    self.chunks_sent.append((operation_id, seq))
                while self._going(operation_id, ws):
                    await self._wait_to_hear()
            except ConnectionClosed:
                pass

    def _going(self, operation_id: str, ws: ClientConnection) -> bool:
        """Whether a transfer still goes: its connection is the laptop's and up, and the server has not ended it."""
        return self._ws is ws and self.connected and operation_id not in self._ended

    async def _wait_to_hear(self) -> None:
        self._heard.clear()
        await self._heard.wait()
