"""The cloud sandbox's workspace: this host's filesystem and shell.

What these methods do is what the tools did before WorkspaceIO existed.
Moving code here must not change what a cloud session sees.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat as stat_module
from collections.abc import AsyncIterator
from pathlib import Path

from surogates.tools.utils.workspace_sandbox import validate_path
from surogates.tools.workspace_io.base import FileStat


class LocalWorkspaceIO:
    """WorkspaceIO over this host, contained to *workspace_path* when one is set."""

    def __init__(self, workspace_path: str | None = None) -> None:
        self.root = workspace_path or None

    async def resolve(self, path: str) -> str:
        if self.root:
            return validate_path(self.root, path)
        return str(Path(os.path.expanduser(path)).resolve())

    async def stat(self, key: str) -> FileStat | None:
        try:
            st = os.stat(key)
        except (OSError, ValueError):
            return None
        return FileStat(
            is_dir=stat_module.S_ISDIR(st.st_mode), size=st.st_size, mtime=st.st_mtime,
        )

    async def read(self, key: str, max_bytes: int | None = None) -> bytes:
        with open(key, "rb") as fh:
            return fh.read(-1 if max_bytes is None else max_bytes)

    async def write(self, key: str, data: bytes) -> None:
        os.makedirs(os.path.dirname(key) or ".", exist_ok=True)
        tmp = key + ".tmp"
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
            with contextlib.suppress(OSError):
                os.chmod(tmp, stat_module.S_IMODE(os.stat(key).st_mode))
            os.replace(tmp, key)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    async def delete(self, key: str) -> None:
        os.unlink(key)

    async def list_dir(self, key: str) -> list[str]:
        return os.listdir(key)

    @contextlib.asynccontextmanager
    async def local_file(self, key: str) -> AsyncIterator[Path]:
        yield Path(key)

    async def which(self, name: str) -> bool:
        return shutil.which(name) is not None
