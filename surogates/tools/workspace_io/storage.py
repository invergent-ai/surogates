"""A cloud session's workspace in object storage: its files, as the api and the harness reach them.

Keys are paths from the workspace's root ("notes/a.md", "" for the root
itself), under the prefix the session's workspace identity names (see
``surogates.session.files``).  What these methods do is what the file panel's
routes did with ``StorageBackend`` before: moving them here changes nothing a
cloud session sees.
"""

from __future__ import annotations

import errno
import os
from collections.abc import Collection
from datetime import datetime
from pathlib import PurePosixPath

from surogates.storage.backend import StorageBackend
from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError
from surogates.tools.workspace_io.base import FileStat, Walk


def _missing(key: str) -> FileNotFoundError:
    return FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), key)


class StorageWorkspaceIO:
    """WorkspaceFiles over a prefix of an agent's bucket."""

    # Keys name objects of the cloud's own: a cache keys a file by its key, as before.
    identity: str | None = None

    def __init__(self, storage: StorageBackend, *, bucket: str, prefix: str) -> None:
        self._storage = storage
        self._bucket = bucket
        self._prefix = prefix

    async def resolve(self, path: str) -> str:
        if path.startswith("/") or ".." in PurePosixPath(path).parts:
            raise WorkspaceSandboxError(f"Path traversal blocked: {path}")
        # As given: the routes used it literally, and an object stored under
        # "./a.md" or "a//b.md" keeps its name.  The root itself is "".
        return "" if path in ("", ".") else path

    async def stat(self, key: str) -> FileStat | None:
        try:
            info = await self._storage.stat(self._bucket, self._prefix + key)
        except KeyError:
            return None
        size = int(info.get("size") or 0)
        modified = info.get("modified")
        mtime = modified.timestamp() if isinstance(modified, datetime) else float(modified or 0)
        return FileStat(is_dir=False, size=size, mtime=mtime, revision=f"{size}:{mtime}")

    async def read(self, key: str, max_bytes: int | None = None) -> bytes:
        try:
            data = await self._storage.read(self._bucket, self._prefix + key)
        except KeyError:
            raise _missing(key) from None
        return data if max_bytes is None else data[:max_bytes]

    async def write(self, key: str, data: bytes, *, expected_revision: str | None = None) -> None:
        # expected_revision is not checked: cloud behaviour does not change.
        await self._storage.write(self._bucket, self._prefix + key, data)

    async def delete(self, key: str) -> None:
        if not await self._storage.exists(self._bucket, self._prefix + key):
            raise _missing(key)
        await self._storage.delete(self._bucket, self._prefix + key)

    async def walk(
        self, key: str, *, skip: Collection[str], skip_top: Collection[str] = (), skip_hidden: bool = False,
    ) -> Walk:
        # One listing holds every key under the prefix, so skipping saves
        # nothing here: what is shown is the caller's to filter, as before.
        base = f"{self._prefix}{key}/" if key else self._prefix
        files = [
            (entry["key"][len(base):], int(entry.get("size") or 0))
            for entry in await self._storage.list_entries(self._bucket, prefix=base)
            if entry["key"] != base
        ]
        return Walk(files, False, None)
