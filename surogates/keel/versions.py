"""A case's file versions: every digest handed to Keel names bytes the runtime still holds.

Keel stores a file's digest, never its bytes, and the file at a path is
written over.  So before a digest leaves the runtime (a write's answer, a
read's header, an output's file value) the bytes it names are kept under
the case's boundary, beside the workspace, by their hash:
``boundaries/case:<id>/versions/<sha256>``.  Never mounted in a sandbox,
never listed as a case file, kept once per case however many paths hold
the bytes.

A version the store would not keep is :class:`VersionNotKept`, and the
caller hands out no digest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from surogates.keel.case_files import case_boundary_folder
from surogates.session.attachment_ingest import workspace_root_id
from surogates.storage.tenant import boundary_workspace_prefix

__all__ = [
    "SHA256_HEX",
    "VERSIONS_FOLDER",
    "CaseVersions",
    "VersionNotKept",
    "case_versions",
    "session_versions",
]

#: The folder of a case's boundary that holds its versions, beside ``workspace/``.
VERSIONS_FOLDER = "versions/"
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_WORKSPACE_FOLDER = "workspace/"


class VersionNotKept(Exception):
    """The store would not keep a version; raised ``from`` the store's own error, whose text can quote a key."""


@dataclass(frozen=True, slots=True)
class CaseVersions:
    """Where one case keeps its versions: a bucket, and a key prefix ending ``versions/``."""

    bucket: str
    prefix: str

    def key(self, sha256: str) -> str:
        """The object key of the version with this digest; ``ValueError`` for anything but a lower-case hex SHA-256."""
        if not SHA256_HEX.fullmatch(sha256):
            raise ValueError("not a SHA-256 digest")
        return self.prefix + sha256

    async def keep(self, storage: Any, sha256: str, data: bytes) -> None:
        """Keep *data*, whose digest is *sha256*, unless the case already holds that version."""
        key = self.key(sha256)
        try:
            if not await storage.exists(self.bucket, key):
                await storage.write(self.bucket, key, data)
        except Exception as exc:
            raise VersionNotKept("the version could not be kept") from exc

    async def keep_file(self, storage: Any, sha256: str, source: Path) -> None:
        """:meth:`keep` the contents of the file *source*, a piece at a time: none of it is held whole."""
        key = self.key(sha256)
        try:
            if not await storage.exists(self.bucket, key):
                await storage.upload(self.bucket, key, source)
        except Exception as exc:
            raise VersionNotKept("the version could not be kept") from exc


def case_versions(settings: Any, case_id: UUID) -> CaseVersions:
    """Where *case_id* keeps its versions, from the runtime's settings."""
    bucket, folder = case_boundary_folder(settings, case_id)
    return CaseVersions(bucket, folder + VERSIONS_FOLDER)


def session_versions(session: Any) -> CaseVersions:
    """Where the case *session* works on keeps its versions: the folder above its workspace, from its own config.

    ``ValueError`` for a session whose workspace is not a boundary's
    ``workspace/`` folder, or with no bucket.
    """
    config = getattr(session, "config", None) or {}
    bucket = config.get("storage_bucket") or ""
    workspace = boundary_workspace_prefix(config, session, workspace_root_id(session))
    if not bucket or not workspace.endswith("/" + _WORKSPACE_FOLDER):
        raise ValueError("the session has no case workspace")
    return CaseVersions(bucket, workspace.removesuffix(_WORKSPACE_FOLDER) + VERSIONS_FOLDER)
