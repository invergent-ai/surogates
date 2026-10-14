"""A folder on a computer, answered as the desktop's file helper answers (desktop/src/files/operations.ts)."""

from __future__ import annotations

import base64
import posixpath
from uuid import uuid4

from surogates.devices.workspace import DeviceWorkspaceIO


class Computer:
    """An operation runner over a dict of files: key -> (bytes, revision). Revisions change on every write.

    *ignores_case* is a folder as macOS and Windows keep one: a key finds the file stored under any case of it.
    """

    def __init__(self, folder: str, files: dict[str, bytes] | None = None, *, ignores_case: bool = False) -> None:
        self.folder = folder
        self.ignores_case = ignores_case
        self.files: dict[str, tuple[bytes, str]] = {}
        self.calls: list[tuple[str, dict]] = []
        self._writes = 0
        for path, data in (files or {}).items():
            self._put(posixpath.join(folder, path), data)

    def _put(self, key: str, data: bytes) -> None:
        self._writes += 1
        self.files[key] = (data, f"1:{self._writes}:{len(data)}:{self._writes}:{self._writes}")

    def _fold(self, key: str) -> str:
        return key.casefold() if self.ignores_case else key

    def _find(self, key: str) -> str:
        """The stored key *key* names: itself as written, or, ignoring case, the one stored under another case."""
        return next((stored for stored in self.files if self._fold(stored) == self._fold(key)), key)

    def _names_under(self, key: str) -> set[str]:
        prefix = self._fold(key.rstrip("/") + "/")
        return {stored[len(prefix):].split("/")[0] for stored in self.files if self._fold(stored).startswith(prefix)}

    async def run(self, kind: str, args: dict, payload: bytes | None = None) -> dict:
        self.calls.append((kind, args))
        if kind == "resolve":
            return {"ok": posixpath.normpath(posixpath.join(self.folder, args["path"]))}
        key = args.get("key")
        if kind == "list_dir":
            if self._find(key) in self.files:
                return {"error": {"type": "os", "code": "ENOTDIR", "message": f"Not a directory: {key}"}}
            names = self._names_under(key)
            if not names and key != self.folder:
                return {"error": {"type": "os", "code": "ENOENT", "message": f"No such directory: {key}"}}
            return {"ok": sorted(names)}
        key = self._find(key) if key is not None else key
        if kind == "stat":
            if key not in self.files:
                return {"ok": {"is_dir": True, "size": 0, "mtime": 1.0, "revision": "dir"} if self._names_under(key) else None}
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


def computer_io(
    folder: str, files: dict[str, bytes] | None = None, device_id: str | None = None, *, ignores_case: bool = False,
) -> DeviceWorkspaceIO:
    """A DeviceWorkspaceIO on a fake computer; its runner is ``io.runner``."""
    return DeviceWorkspaceIO(
        Computer(folder, files, ignores_case=ignores_case), root=folder, identity=f"device:{device_id or uuid4()}",
    )
