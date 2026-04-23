"""ArtifactStore — persistence for chat-embedded artifacts.

Artifacts live in the session bucket under ``artifacts/{artifact_id}/``.
Each version is a separate object so history is preserved; the newest
version's metadata is tracked in ``artifacts/{artifact_id}/meta.json``.
A session-level index at ``artifacts/index.json`` lists every artifact
in creation order so the UI can enumerate them without listing keys.

Key layout inside the session bucket::

    artifacts/
    ├── index.json                        # ordered list of ArtifactMeta
    └── {artifact_id}/
        ├── meta.json                     # latest ArtifactMeta
        ├── v1.json                       # serialised spec for version 1
        └── v2.json                       # serialised spec for version 2 (if updated)

Payloads are JSON: ``{"kind": "chart", "spec": {...}}``.  The API server
has the session bucket credentials; the worker calls it via
:class:`HarnessAPIClient`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID, uuid4

from surogates.artifacts.models import (
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACTS_PER_SESSION,
    ArtifactKind,
    ArtifactMeta,
)
from surogates.storage.backend import StorageBackend

logger = logging.getLogger(__name__)


_ARTIFACTS_PREFIX = "artifacts/"
_INDEX_KEY = "artifacts/index.json"


class ArtifactLimitError(Exception):
    """Raised when an artifact exceeds a configured limit."""


class ArtifactNotFoundError(KeyError):
    """Raised when an artifact (or a specific version) is missing."""


class ArtifactStore:
    """Session-scoped artifact persistence.

    Parameters
    ----------
    backend:
        Object-storage backend for the session bucket.
    session_id:
        The session this store is scoped to.  Used only for metadata —
        the bucket name is passed in explicitly so callers already
        resolving the bucket for other workspace operations don't pay a
        second lookup.
    bucket:
        The session bucket (``session-{session_id}``).
    """

    def __init__(
        self,
        backend: StorageBackend,
        *,
        session_id: UUID,
        bucket: str,
    ) -> None:
        self._backend = backend
        self._session_id = session_id
        self._bucket = bucket

    # ------------------------------------------------------------------
    # Bucket / key helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _dir_key(artifact_id: UUID) -> str:
        return f"{_ARTIFACTS_PREFIX}{artifact_id}"

    @staticmethod
    def _meta_key(artifact_id: UUID) -> str:
        return f"{_ARTIFACTS_PREFIX}{artifact_id}/meta.json"

    @staticmethod
    def _version_key(artifact_id: UUID, version: int) -> str:
        return f"{_ARTIFACTS_PREFIX}{artifact_id}/v{version}.json"

    # ------------------------------------------------------------------
    # Index
    # ------------------------------------------------------------------

    async def _read_index(self) -> list[dict]:
        """Return the session's artifact index, empty if absent."""
        try:
            raw = await self._backend.read_text(self._bucket, _INDEX_KEY)
        except KeyError:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "artifact index for session %s is corrupted — resetting",
                self._session_id,
            )
            return []
        return parsed if isinstance(parsed, list) else []

    async def _write_index(self, entries: list[dict]) -> None:
        await self._backend.write_text(
            self._bucket, _INDEX_KEY, json.dumps(entries, default=str),
        )

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def create(
        self, *, name: str, kind: ArtifactKind, spec: dict,
    ) -> ArtifactMeta:
        """Create a new artifact at version 1.

        Raises :class:`ArtifactLimitError` if the session is at the
        artifact cap or the payload exceeds the per-artifact byte limit.
        """
        payload = json.dumps({"kind": kind.value, "spec": spec})
        size = len(payload.encode("utf-8"))
        if size > MAX_ARTIFACT_BYTES:
            raise ArtifactLimitError(
                f"artifact payload {size} bytes exceeds limit {MAX_ARTIFACT_BYTES}",
            )

        index = await self._read_index()
        if len(index) >= MAX_ARTIFACTS_PER_SESSION:
            raise ArtifactLimitError(
                f"session has {len(index)} artifacts (limit {MAX_ARTIFACTS_PER_SESSION})",
            )

        artifact_id = uuid4()
        meta = ArtifactMeta.new(
            artifact_id=artifact_id,
            session_id=self._session_id,
            name=name,
            kind=kind,
            version=1,
            size=size,
        )

        await asyncio.gather(
            self._backend.write_text(
                self._bucket, self._version_key(artifact_id, 1), payload,
            ),
            self._backend.write_text(
                self._bucket, self._meta_key(artifact_id), meta.model_dump_json(),
            ),
        )

        index.append(meta.model_dump(mode="json"))
        await self._write_index(index)

        return meta

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def list(self) -> list[ArtifactMeta]:
        """Return all artifacts for the session, in creation order."""
        index = await self._read_index()
        return [ArtifactMeta.model_validate(entry) for entry in index]

    async def get_meta(self, artifact_id: UUID) -> ArtifactMeta:
        """Fetch metadata for a single artifact."""
        try:
            raw = await self._backend.read_text(
                self._bucket, self._meta_key(artifact_id),
            )
        except KeyError as exc:
            raise ArtifactNotFoundError(str(artifact_id)) from exc
        return ArtifactMeta.model_validate_json(raw)

    async def get_payload(
        self, artifact_id: UUID, version: int | None = None,
    ) -> dict:
        """Fetch the payload ``{"kind", "spec"}`` for a specific version.

        When ``version`` is omitted, returns the latest version recorded
        in metadata.
        """
        if version is None:
            meta = await self.get_meta(artifact_id)
            version = meta.version
        try:
            raw = await self._backend.read_text(
                self._bucket, self._version_key(artifact_id, version),
            )
        except KeyError as exc:
            raise ArtifactNotFoundError(
                f"{artifact_id} v{version}",
            ) from exc
        return json.loads(raw)
