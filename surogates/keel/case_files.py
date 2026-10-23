"""A case's files: the workspace every step session of the case shares, reached with no session.

Keel lists, reads and writes them through keel-link.  The workspace is the
``case:<case id>`` boundary's, in the runtime's session bucket: the bucket
and the prefix a step session of the case resolves, built by the same
functions, so a file one side writes is the file the other reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from botocore.exceptions import ClientError

from surogates.channels.memory_boundary import CASE_BOUNDARY_PREFIX
from surogates.storage.tenant import agent_session_bucket, boundary_workspace_prefix, workspace_session_shim
from surogates.tools.workspace_io import StorageWorkspaceIO

__all__ = [
    "CaseFile",
    "CaseFileError",
    "INVALID_PATH",
    "case_path",
    "case_workspace",
    "download_case_file",
    "list_case_files",
    "write_case_file",
]

INVALID_PATH = "invalid-path"


class CaseFileError(ValueError):
    """What Keel named is not a file's path in a case's workspace; ``code`` says why."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class CaseFile:
    """One file of a case's workspace, as its listing names it."""

    path: str
    size: int
    modified_at: datetime


def case_path(path: str) -> str:
    """*path*, relative to a case's workspace and ``/``-separated, without its ``.`` and empty parts.

    Raises ``CaseFileError("invalid-path")`` for an empty path, one holding a
    NUL, an absolute one, and one with a ``..`` part.  Nothing else changes:
    no root is taken off, so the path names the file Keel named.
    """
    if not path or "\x00" in path or path.startswith("/"):
        raise CaseFileError(INVALID_PATH)
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        raise CaseFileError(INVALID_PATH)
    return "/".join(parts)


def _bucket_and_prefix(settings: Any, case_id: UUID) -> tuple[str, str]:
    """Where *case_id*'s workspace is: the bucket, and the key prefix a step session of the case resolves."""
    config = {
        "workspace_boundary": f"{CASE_BOUNDARY_PREFIX}{case_id}",
        # As a session is stamped when it is made: the installation's prefix.
        "storage_key_prefix": getattr(settings.storage, "key_prefix", "") or "",
    }
    prefix = boundary_workspace_prefix(config, workspace_session_shim(config, case_id), case_id)
    return agent_session_bucket(settings.storage.bucket), prefix


def case_workspace(storage: Any, settings: Any, case_id: UUID) -> StorageWorkspaceIO:
    """*case_id*'s workspace, as its step sessions reach it."""
    bucket, prefix = _bucket_and_prefix(settings, case_id)
    return StorageWorkspaceIO(storage, bucket=bucket, prefix=prefix)


def _no_bucket(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "NoSuchBucket"


def _moment(modified: Any) -> datetime:
    """A listing's ``modified``, a date on S3 and a POSIX time on a disk, as a date in UTC."""
    if isinstance(modified, datetime):
        return modified.astimezone(timezone.utc) if modified.tzinfo is not None else modified.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(float(modified or 0), tz=timezone.utc)


async def list_case_files(storage: Any, settings: Any, case_id: UUID) -> list[CaseFile]:
    """Every file in *case_id*'s workspace, by path.

    None for a case no session has written to yet, nor before the bucket
    itself is made.
    """
    bucket, prefix = _bucket_and_prefix(settings, case_id)
    try:
        entries = await storage.list_entries(bucket, prefix=prefix)
    except ClientError as exc:
        if _no_bucket(exc):
            return []
        raise
    files = [
        CaseFile(path=entry["key"][len(prefix):], size=int(entry.get("size") or 0), modified_at=_moment(entry.get("modified")))
        for entry in entries
        # A folder's marker is no file.
        if entry["key"].startswith(prefix) and len(entry["key"]) > len(prefix) and not entry["key"].endswith("/")
    ]
    return sorted(files, key=lambda file: file.path)


async def download_case_file(storage: Any, settings: Any, case_id: UUID, path: str, target: Path) -> int:
    """Copy the file at *path* in *case_id*'s workspace into *target*, a piece at a time; its size.

    Raises :class:`FileNotFoundError` when no file is there.
    """
    bucket, prefix = _bucket_and_prefix(settings, case_id)
    try:
        return await storage.download(bucket, prefix + path, target)
    except KeyError:
        raise FileNotFoundError(path) from None
    except ClientError as exc:
        if _no_bucket(exc):
            raise FileNotFoundError(path) from None
        raise


async def write_case_file(storage: Any, settings: Any, case_id: UUID, path: str, data: bytes) -> None:
    """Write *data* at *path* in *case_id*'s workspace, over any file there.

    The bucket is made first: Keel may write a case's files before its first
    step session makes it.
    """
    workspace = case_workspace(storage, settings, case_id)
    await storage.create_bucket(agent_session_bucket(settings.storage.bucket))
    await workspace.write(path, data)
