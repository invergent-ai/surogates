"""A laptop in a test: runs device operations the way Surogate Desktop does.

``perform`` is the laptop's half of the operation contract documented in
``surogates.devices.workspace``, including its size caps: it runs one
operation against a WorkspaceIO standing in for the bound folder and returns
the outcome the app would send.
"""

from __future__ import annotations

import base64
import errno
import json
from typing import Any

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
