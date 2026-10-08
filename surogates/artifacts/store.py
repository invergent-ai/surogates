"""ArtifactStore — persistence for chat-embedded artifacts.

Artifacts live in the session's files (``surogates.session.files``): in a
cloud session's workspace under ``_artifacts/{artifact_id}/``, whose leading
underscore marks it server-internal so the workspace file browser and REST
layer can hide and reject access to it (see
:data:`surogates.api.routes.workspace._RESERVED_PREFIXES`); in a local folder
under ``.surogates-results/artifacts/{root session id}/``, among the harness's
own files there, which Ask every time does not ask about.  A folder outlives
its chats, so each root chat keeps its own there, its sub-agents sharing
them, as a cloud workspace is its root chat's.  The layout below is the same
in both.

Each version is a separate object so history is preserved; the newest
version's metadata is tracked in ``_artifacts/{artifact_id}/meta.json``.
A session-level index at ``_artifacts/index.json`` lists every
artifact in creation order so the UI can enumerate them without
listing keys.

Key layout inside the session workspace::

    _artifacts/
    ├── index.json                        # ordered list of ArtifactMeta
    └── {artifact_id}/
        ├── meta.json                     # latest ArtifactMeta
        ├── v1.json                       # serialised spec for version 1
        └── v2.json                       # serialised spec for version 2 (if updated)

Payloads are JSON: ``{"kind": "chart", "spec": {...}}``.  A cloud
session's worker makes its artifacts through the API server, via
:class:`HarnessAPIClient`.  A local folder's are made by the tool call that
makes them, through :class:`FolderArtifacts`, so their operations are
journaled under that call.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from surogates.artifacts.models import (
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACTS_PER_SESSION,
    ArtifactKind,
    ArtifactMeta,
    ArtifactSpec,
)
from surogates.devices.workspace import DeviceWorkspaceIO
from surogates.session.events import EventType
from surogates.tools.utils.tool_result_storage import WORKSPACE_STORAGE_DIR, keep_out_of_git
from surogates.tools.workspace_io import WorkspaceFiles

logger = logging.getLogger(__name__)


# Where a session's artifacts are in its files: a cloud workspace's own
# folder, or, in a local folder, the harness's, a folder in it per root chat.
CLOUD_ARTIFACTS = "_artifacts"
FOLDER_ARTIFACTS = f"{WORKSPACE_STORAGE_DIR}/artifacts"
_INDEX = "index.json"
# How much of a local folder's artifact file is read: anything on its computer
# may write there, and no file the store writes is longer than an artifact,
# so one longer than that is cut, and read as corrupted.
_FOLDER_READ_BYTES = MAX_ARTIFACT_BYTES + 1


class ArtifactLimitError(Exception):
    """Raised when an artifact exceeds a configured limit."""


class ArtifactNotFoundError(KeyError):
    """Raised when an artifact (or a specific version) is missing."""


class ArtifactStore:
    """Session-scoped artifact persistence, in the session's files.

    *files* is what ``surogates.session.files.session_files`` gives for the
    session, or a tool call's own ``workspace_io`` on a local folder.
    *session_id* is used only for metadata.  *root* is the chat's root
    session (``sandbox_session_key``), whose folder of artifacts a local
    folder's are kept in; a cloud workspace is the root's already.
    """

    def __init__(self, files: WorkspaceFiles, *, session_id: UUID, root: str | None = None) -> None:
        self._files = files
        self._session_id = session_id
        self._on_device = isinstance(files, DeviceWorkspaceIO)
        if not self._on_device:
            self._folder = CLOUD_ARTIFACTS
        elif root:
            # A chat's id: nothing else may name a folder under the harness's.
            self._folder = f"{FOLDER_ARTIFACTS}/{UUID(root)}"
        else:
            raise ValueError("A local folder keeps its artifacts per root chat: name the chat's root")

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _meta_path(artifact_id: UUID) -> str:
        return f"{artifact_id}/meta.json"

    @staticmethod
    def _version_path(artifact_id: UUID, version: int) -> str:
        return f"{artifact_id}/v{version}.json"

    async def _read(self, path: str) -> str:
        """The text of *path* under the artifacts folder.  Raises ``FileNotFoundError``."""
        key = await self._files.resolve(f"{self._folder}/{path}")
        if not self._on_device:
            return (await self._files.read(key)).decode("utf-8")
        data = await self._files.read(key, max_bytes=_FOLDER_READ_BYTES)
        # Cut: corrupted, as an empty file is.
        return "" if len(data) >= _FOLDER_READ_BYTES else data.decode("utf-8")

    async def _write(self, path: str, text: str) -> None:
        await keep_out_of_git(self._files)
        key = await self._files.resolve(f"{self._folder}/{path}")
        await self._files.write(key, text.encode("utf-8"))

    async def _write_all(self, *files: tuple[str, str]) -> None:
        """Write each ``(path, text)``: in the cloud at once, as always; on a local
        folder one after another, as each is an operation of the tool call,
        numbered in the order it asks for them."""
        if not self._on_device:
            await asyncio.gather(*(self._write(path, text) for path, text in files))
            return
        for path, text in files:
            await self._write(path, text)

    # ------------------------------------------------------------------
    # Index
    # ------------------------------------------------------------------

    async def _read_index(self) -> list[dict]:
        """Return the session's artifact index, empty if absent."""
        try:
            raw = await self._read(_INDEX)
        except FileNotFoundError:
            return []
        except UnicodeDecodeError:
            # Not text: corrupted, as an index that is not JSON is.
            raw = ""
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "artifact index for session %s is corrupted — resetting",
                self._session_id,
            )
            return []
        if not isinstance(parsed, list):
            return []
        if not self._on_device:
            return parsed
        # A local folder's index is anyone's on its computer to write: an
        # entry that is no artifact's is passed over, not the chat's artifacts.
        kept = [entry for entry in parsed if _is_meta(entry)]
        if len(kept) < len(parsed):
            logger.warning(
                "artifact index for session %s has %d entries that are not artifacts — passing them over",
                self._session_id, len(parsed) - len(kept),
            )
        return kept

    async def _write_index(self, entries: list[dict]) -> None:
        await self._write(_INDEX, json.dumps(entries, default=str))

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

        await self._write_all(
            (self._version_path(artifact_id, 1), payload),
            (self._meta_path(artifact_id), meta.model_dump_json()),
        )

        index.append(meta.model_dump(mode="json"))
        await self._write_index(index)

        return meta

    async def update(
        self, artifact_id: UUID, *, name: str, kind: ArtifactKind, spec: dict,
    ) -> ArtifactMeta:
        """Write a new version of an existing artifact.

        The whole payload is replaced -- an artifact version is a
        snapshot, not a diff -- and the previous ``v{n}.json`` is left in
        place, which is what makes the version history in the key layout
        real rather than nominal.

        The per-session artifact cap is deliberately not re-checked: a
        revision consumes an index slot that is already spent, and
        refusing to revise at the cap would strand the user on a wrong
        artifact with no way to fix it.

        Raises :class:`ArtifactNotFoundError` if the artifact does not
        exist, and :class:`ArtifactLimitError` if the new payload is over
        the per-artifact byte limit.
        """
        previous = await self.get_meta(artifact_id)

        payload = json.dumps({"kind": kind.value, "spec": spec})
        size = len(payload.encode("utf-8"))
        if size > MAX_ARTIFACT_BYTES:
            raise ArtifactLimitError(
                f"artifact payload {size} bytes exceeds limit {MAX_ARTIFACT_BYTES}",
            )

        version = previous.version + 1
        meta = ArtifactMeta(
            artifact_id=artifact_id,
            session_id=self._session_id,
            name=name,
            kind=kind,
            version=version,
            size=size,
            # Creation time is a property of the artifact, not of the
            # revision; the UI orders the thread by event, not by this.
            created_at=previous.created_at,
        )

        await self._write_all(
            (self._version_path(artifact_id, version), payload),
            (self._meta_path(artifact_id), meta.model_dump_json()),
        )

        index = await self._read_index()
        entry = meta.model_dump(mode="json")
        for position, existing in enumerate(index):
            if str(existing.get("artifact_id")) == str(artifact_id):
                index[position] = entry
                break
        else:
            # Metadata exists but the index lost the entry (a corrupted
            # index resets to empty). Re-add rather than silently
            # dropping the artifact from the UI's enumeration.
            index.append(entry)
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
            raw = await self._read(self._meta_path(artifact_id))
        except FileNotFoundError as exc:
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
            raw = await self._read(self._version_path(artifact_id, version))
        except FileNotFoundError as exc:
            raise ArtifactNotFoundError(
                f"{artifact_id} v{version}",
            ) from exc
        return json.loads(raw)


def _is_meta(entry: Any) -> bool:
    try:
        ArtifactMeta.model_validate(entry)
    except ValidationError:
        return False
    return True


def artifact_event(meta: ArtifactMeta) -> dict[str, Any]:
    """What ``artifact.created`` and ``artifact.updated`` carry: the metadata, never the spec."""
    return {
        "artifact_id": str(meta.artifact_id),
        "name": meta.name,
        "kind": meta.kind.value,
        "version": meta.version,
        "size": meta.size,
    }


class FolderArtifacts:
    """A local folder's artifacts, for its tools: the api client's two artifact calls, made in the folder.

    A chat on a local folder makes its artifacts through the tool call that
    makes them, so their operations are journaled under that call, and a
    resumed call is reported interrupted, as any harness tool is.  It checks
    what the artifact routes check, answers in the api client's shapes, and
    emits the event the routes emit for a cloud chat.
    """

    def __init__(self, files: WorkspaceFiles, session_store: Any, session_id: UUID, root: str) -> None:
        self._artifacts = ArtifactStore(files, session_id=session_id, root=root)
        self._session_store = session_store
        self._session_id = session_id

    async def create_artifact(
        self, *, name: str, kind: str, spec: dict[str, Any], artifact_id: str | None = None,
    ) -> str:
        """Create an artifact, or a new version of *artifact_id*: as ``HarnessAPIClient.create_artifact``."""
        try:
            checked = ArtifactSpec(name=name, kind=ArtifactKind(kind), spec=spec)
            checked.validate_spec()
            if artifact_id:
                meta = await self._artifacts.update(UUID(artifact_id), name=name, kind=checked.kind, spec=spec)
            else:
                meta = await self._artifacts.create(name=name, kind=checked.kind, spec=spec)
        except ArtifactNotFoundError:
            return json.dumps({"success": False, "error": f"Artifact {artifact_id} not found."}, ensure_ascii=False)
        except (ArtifactLimitError, ValidationError, ValueError) as exc:
            return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
        await self._session_store.emit_event(
            self._session_id,
            EventType.ARTIFACT_UPDATED if artifact_id else EventType.ARTIFACT_CREATED,
            artifact_event(meta),
        )
        return json.dumps({"success": True, **meta.model_dump(mode="json")}, ensure_ascii=False)

    async def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        """``{meta, kind, spec}`` of *artifact_id*'s latest version, or None: as ``HarnessAPIClient.get_artifact``."""
        try:
            meta = await self._artifacts.get_meta(UUID(artifact_id))
            payload = await self._artifacts.get_payload(meta.artifact_id, version=meta.version)
        except (ArtifactNotFoundError, ValueError):
            return None
        return {"meta": meta.model_dump(mode="json"), "kind": payload["kind"], "spec": payload["spec"]}
