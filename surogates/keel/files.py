"""A file in a case's workspace, as Keel takes it in a step's output.

A step's file field holds files the step's sessions wrote to the case's
workspace.  The model names each by its path there, and only a file the
case's files show (``surogates.keel.case_files``); Keel gets
``{"name", "uri", "sha256"}``: the file's base name, a ``case-file://`` URI
of the case and the path, and the digest of the bytes stored.
"""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Any

from surogates.channels.channel_media import normalize_workspace_path
from surogates.keel.case_files import WORKSPACE_ROOTS, case_path
from surogates.session.attachment_ingest import workspace_root_id
from surogates.storage.tenant import boundary_workspace_key

__all__ = ["case_file_value", "file_value", "workspace_path"]


def workspace_path(path: str) -> str | None:
    """*path* relative to the workspace, or ``None`` for a path outside it: one that climbs out, or another root."""
    raw = (path or "").strip()
    if raw.startswith("/") and not raw.startswith(WORKSPACE_ROOTS):
        return None
    return normalize_workspace_path(raw)


async def case_file_value(storage: Any, session: Any, case_id: object, path: str) -> dict[str, str]:
    """The Keel file value of the file at *path* in *session*'s workspace.

    Raises :class:`ValueError` for a path outside the workspace;
    :class:`~surogates.keel.case_files.CaseFileError`, a ``ValueError``, for
    one the case's files never show, such as a hidden file or folder or the
    platform's own; and :class:`FileNotFoundError` when no file is there.
    """
    relative = workspace_path(path)
    if relative is None:
        raise ValueError("not a path in the workspace")
    relative = case_path(relative)
    config = getattr(session, "config", None) or {}
    bucket = config.get("storage_bucket") or ""
    if storage is None or not bucket:
        raise FileNotFoundError(relative)
    key = boundary_workspace_key(config, session, workspace_root_id(session), relative)
    try:
        data = await storage.read(bucket, key)
    except KeyError:
        raise FileNotFoundError(relative) from None
    return file_value(case_id, relative, data)


def file_value(case_id: object, relative: str, data: bytes) -> dict[str, str]:
    """The Keel file value of *data*, stored at *relative* in the case's workspace."""
    # An empty file is a file: its digest is the empty bytes'.
    return {
        "name": PurePosixPath(relative).name,
        "uri": f"case-file://{case_id}/{relative}",
        "sha256": hashlib.sha256(data).hexdigest(),
    }
