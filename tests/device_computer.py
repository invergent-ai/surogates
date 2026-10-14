"""A folder on a computer, answered as the desktop's file helper answers (desktop/src/files/operations.ts)."""

from __future__ import annotations

import base64
import posixpath
from uuid import uuid4

from surogates.devices.workspace import DeviceWorkspaceIO


class Computer:
    """An operation runner over a dict of files: key -> (bytes, revision). Revisions change on every write."""

    def __init__(self, folder: str, files: dict[str, bytes] | None = None) -> None:
        self.folder = folder
        self.files: dict[str, tuple[bytes, str]] = {}
        self.calls: list[tuple[str, dict]] = []
        self._writes = 0
        for path, data in (files or {}).items():
            self._put(posixpath.join(folder, path), data)

    def _put(self, key: str, data: bytes) -> None:
        self._writes += 1
        self.files[key] = (data, f"1:{self._writes}:{len(data)}:{self._writes}:{self._writes}")

    async def run(self, kind: str, args: dict, payload: bytes | None = None) -> dict:
        self.calls.append((kind, args))
        if kind == "resolve":
            return {"ok": posixpath.normpath(posixpath.join(self.folder, args["path"]))}
        key = args.get("key")
        if kind == "stat":
            if key not in self.files:
                return {"ok": None}
            data, revision = self.files[key]
            return {"ok": {"is_dir": False, "size": len(data), "mtime": 1.0, "revision": revision}}
        if kind == "read":
            if key not in self.files:
                return {"error": {"type": "os", "code": "ENOENT", "message": f"No such file: {key}"}}
            return {"ok": base64.b64encode(self.files[key][0]).decode("ascii")}
        if kind == "write":
            data = payload if payload is not None else base64.b64decode(args["data"])
            if args.get("create") is True:
                if key in self.files:
                    return {"error": {"type": "os", "code": "EEXIST", "message": f"File exists: {key}"}}
            elif "expected_revision" in args and (key not in self.files or self.files[key][1] != args["expected_revision"]):
                return {"error": {"type": "conflict", "message": f"{key} changed on the computer"}}
            self._put(key, data)
            return {"ok": None}
        raise AssertionError(f"the fake computer does not do {kind}")


def computer_io(folder: str, files: dict[str, bytes] | None = None, device_id: str | None = None) -> DeviceWorkspaceIO:
    """A DeviceWorkspaceIO on a fake computer; its runner is ``io.runner``."""
    return DeviceWorkspaceIO(Computer(folder, files), root=folder, identity=f"device:{device_id or uuid4()}")
