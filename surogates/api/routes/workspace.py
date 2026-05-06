"""Workspace file browsing for sessions.

Exposes the session's workspace via ``StorageBackend`` so the web UI can
display a workspace panel alongside the chat thread.  Works with both
``LocalBackend`` (dev) and ``S3Backend`` (production, Garage/S3).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel

from surogates.session.store import SessionNotFoundError, SessionStore
from surogates.storage.backend import StorageBackend
from surogates.storage.tenant import session_workspace_key, session_workspace_prefix
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

_MAX_LIST_DEPTH = 12
_MAX_ENTRIES = 5000
_MAX_READ_BYTES = 512_000  # 500 KB
_MAX_UPLOAD_BYTES = 50_000_000  # 50 MB
_MAX_DOWNLOAD_BYTES = 100_000_000  # 100 MB

# Extensions considered "text" for in-browser viewing.
_TEXT_EXTENSIONS = frozenset({
    ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml",
    ".toml", ".cfg", ".ini", ".env", ".md", ".rst", ".txt", ".csv",
    ".html", ".css", ".scss", ".less", ".xml", ".svg", ".sql",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd",
    ".rs", ".go", ".java", ".kt", ".c", ".cpp", ".h", ".hpp",
    ".rb", ".php", ".lua", ".pl", ".r", ".jl", ".ex", ".exs",
    ".zig", ".nim", ".v", ".d", ".swift", ".m", ".mm",
    ".dockerfile", ".tf", ".hcl", ".nix", ".dhall",
    ".graphql", ".proto", ".lock", ".editorconfig", ".gitignore",
    ".gitattributes", ".dockerignore", ".prettierrc", ".eslintrc",
})

# Names that are always text regardless of extension.
_TEXT_NAMES = frozenset({
    "Makefile", "Dockerfile", "Procfile", "Vagrantfile", "Gemfile",
    "Rakefile", "Justfile", "CMakeLists.txt", "LICENSE", "LICENCE",
    "AGENTS.md", "CLAUDE.md", ".cursorrules",
})

# Extensions considered "image" for in-browser viewing (served as base64).
_IMAGE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico",
    ".avif", ".tiff", ".tif",
})

# Maximum raw bytes for image files served inline as base64.
_MAX_IMAGE_BYTES = 10_000_000  # 10 MB


# Directories to skip when building the tree.
_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", ".nox", ".eggs",
    "dist", "build", ".next", ".nuxt", ".output", ".turbo",
    "venv", ".venv", "env", ".env",
})

# Session-bucket key prefixes reserved for server-side storage.  The
# leading underscore marks these as internal: artifact metadata and
# payloads live under ``_artifacts/{id}/``.  Hidden from the workspace
# tree and blocked from read/write/delete so users can't see or mutate
# internal state through the file-browser panel.
_RESERVED_PREFIXES: tuple[str, ...] = ("_artifacts/",)


def _is_reserved(key: str) -> bool:
    """Return True if ``key`` points into a reserved internal prefix."""
    return any(key.startswith(p) for p in _RESERVED_PREFIXES)


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class FileEntry(BaseModel):
    """A single file or directory entry in the workspace tree."""

    name: str
    path: str
    kind: Literal["file", "dir"]
    size: int | None = None
    children: list["FileEntry"] | None = None


class WorkspaceTreeResponse(BaseModel):
    """Full recursive workspace tree."""

    root: str
    entries: list[FileEntry]
    truncated: bool = False


class FileContentResponse(BaseModel):
    """Content of a single workspace file.

    For text files ``encoding`` is ``"utf-8"`` (default) and ``content``
    contains the raw text.  For image files ``encoding`` is ``"base64"``
    and ``content`` holds the base64-encoded bytes.
    """

    path: str
    content: str
    size: int
    mime_type: str | None = None
    encoding: Literal["utf-8", "base64"] = "utf-8"
    truncated: bool = False


class UploadResponse(BaseModel):
    """Result of uploading a file to the workspace."""

    path: str
    size: int


class DeleteResponse(BaseModel):
    """Result of deleting a file from the workspace."""

    path: str
    deleted: bool = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_session_store(request: Request) -> SessionStore:
    """Retrieve the SessionStore from app state."""
    store: SessionStore | None = getattr(request.app.state, "session_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session store not available.",
        )
    return store


def _get_storage(request: Request) -> StorageBackend:
    """Retrieve the StorageBackend from app state."""
    return request.app.state.storage


def _require_service_account_api_route(
    request: Request,
    tenant: TenantContext,
) -> None:
    """For ``/v1/api/*`` aliases, require a service-account principal."""
    if (
        request.url.path.startswith("/v1/api/")
        and tenant.service_account_id is None
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires a service-account token.",
        )


async def _get_storage_bucket(
    store: SessionStore, session_id: UUID, tenant: TenantContext,
) -> str:
    """Resolve and validate the agent bucket for workspace access."""
    try:
        session = await store.get_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )

    if not tenant.owns_session(session.org_id, session_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )

    bucket = session.config.get("storage_bucket")
    if not bucket:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Session {session_id} has no agent bucket.",
        )
    return bucket


def _strip_session_prefix(session_id: UUID, key: str) -> str:
    prefix = session_workspace_prefix(session_id)
    if not key.startswith(prefix):
        return key
    return key[len(prefix):]


def _is_text_key(key: str) -> bool:
    """Heuristic: is this key likely a text file?"""
    name = PurePosixPath(key).name
    if name in _TEXT_NAMES:
        return True
    ext = PurePosixPath(key).suffix.lower()
    if ext in _TEXT_EXTENSIONS:
        return True
    mime, _ = mimetypes.guess_type(key)
    if mime and mime.startswith("text/"):
        return True
    return False


def _is_image_key(key: str) -> bool:
    """Heuristic: is this key an image file we can display inline?"""
    ext = PurePosixPath(key).suffix.lower()
    return ext in _IMAGE_EXTENSIONS


def _should_skip_dir(dirname: str) -> bool:
    """Should this directory be skipped in the tree listing?"""
    if dirname.startswith(".") and dirname not in (".github", ".vscode"):
        return True
    return dirname in _SKIP_DIRS


def _validate_path(path: str) -> None:
    """Reject path traversal and reserved-prefix access."""
    parts = PurePosixPath(path).parts
    if ".." in parts:
        raise HTTPException(status_code=403, detail="Path traversal not allowed.")
    if path.startswith("/"):
        raise HTTPException(status_code=403, detail="Absolute paths not allowed.")
    if _is_reserved(path):
        raise HTTPException(
            status_code=403,
            detail="This path is reserved for internal storage.",
        )


def _build_tree(keys: list[str]) -> list[FileEntry]:
    """Build a nested FileEntry tree from a flat list of S3 keys.

    Filters out hidden/noise directories and respects depth/entry limits.
    """
    # Build a dict tree structure first.
    # Dict values are either nested dicts (directories) or strings (leaf files).
    tree: dict = {}
    for key in keys:
        parts = PurePosixPath(key).parts
        if not parts:
            continue
        node = tree
        for part in parts[:-1]:
            existing = node.get(part)
            if existing is None:
                node[part] = {}
            elif isinstance(existing, str):
                # Collision: a file path is also a directory prefix — upgrade to dict.
                node[part] = {}
            node = node[part]
        # Leaf node: only set if not already a directory.
        leaf = parts[-1]
        if leaf not in node or isinstance(node[leaf], str):
            node[leaf] = key

    def _convert(subtree: dict, prefix: str, depth: int, counter: list[int]) -> list[FileEntry]:
        if depth > _MAX_LIST_DEPTH:
            return []
        entries: list[FileEntry] = []
        for name in sorted(subtree.keys(), key=lambda n: (isinstance(subtree[n], str), n.lower())):
            if counter[0] >= _MAX_ENTRIES:
                break
            value = subtree[name]
            rel_path = f"{prefix}/{name}" if prefix else name

            if isinstance(value, dict):
                # Directory
                if _should_skip_dir(name):
                    continue
                counter[0] += 1
                children = _convert(value, rel_path, depth + 1, counter)
                entries.append(FileEntry(name=name, path=rel_path, kind="dir", children=children))
            else:
                # File
                counter[0] += 1
                entries.append(FileEntry(name=name, path=rel_path, kind="file"))

        return entries

    counter = [0]
    result = _convert(tree, "", 0, counter)
    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/api/sessions/{session_id}/workspace/tree",
    response_model=WorkspaceTreeResponse,
)
@router.get(
    "/sessions/{session_id}/workspace/tree",
    response_model=WorkspaceTreeResponse,
)
async def get_workspace_tree(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> WorkspaceTreeResponse:
    """Return the recursive file tree for a session's workspace."""
    _require_service_account_api_route(request, tenant)
    store = _get_session_store(request)
    storage = _get_storage(request)
    bucket = await _get_storage_bucket(store, session_id, tenant)

    prefix = session_workspace_prefix(session_id)
    keys = await storage.list_keys(bucket, prefix=prefix)
    relative_keys = [_strip_session_prefix(session_id, k) for k in keys]
    # Drop keys living under reserved prefixes (artifact storage) so
    # internal server-side files don't leak into the workspace browser.
    visible_keys = [k for k in relative_keys if k and not _is_reserved(k)]
    entries = _build_tree(visible_keys)
    truncated = len(visible_keys) >= _MAX_ENTRIES

    return WorkspaceTreeResponse(
        root=bucket,
        entries=entries,
        truncated=truncated,
    )


@router.get(
    "/api/sessions/{session_id}/workspace/file",
    response_model=FileContentResponse,
)
@router.get(
    "/sessions/{session_id}/workspace/file",
    response_model=FileContentResponse,
)
async def get_workspace_file(
    session_id: UUID,
    request: Request,
    path: str = Query(..., description="Relative path within the workspace"),
    tenant: TenantContext = Depends(get_current_tenant),
) -> FileContentResponse:
    """Read the content of a single file in the session's workspace."""
    _require_service_account_api_route(request, tenant)
    _validate_path(path)
    store = _get_session_store(request)
    storage = _get_storage(request)
    bucket = await _get_storage_bucket(store, session_id, tenant)

    is_text = _is_text_key(path)
    is_image = _is_image_key(path)

    if not is_text and not is_image:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Binary files cannot be viewed in the workspace panel.",
        )

    try:
        data = await storage.read(bucket, session_workspace_key(session_id, path))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"File not found: {path}")

    size = len(data)
    mime, _ = mimetypes.guess_type(path)

    if is_image:
        if size > _MAX_IMAGE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Image too large to preview ({size} bytes, limit {_MAX_IMAGE_BYTES}).",
            )
        content = base64.b64encode(data).decode("ascii")
        return FileContentResponse(
            path=path,
            content=content,
            size=size,
            mime_type=mime or "application/octet-stream",
            encoding="base64",
            truncated=False,
        )

    # Text file path.
    truncated = size > _MAX_READ_BYTES
    content = data[:_MAX_READ_BYTES].decode("utf-8", errors="replace")

    return FileContentResponse(
        path=path,
        content=content,
        size=size,
        mime_type=mime,
        encoding="utf-8",
        truncated=truncated,
    )


@router.post(
    "/api/sessions/{session_id}/workspace/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
)
@router.post(
    "/sessions/{session_id}/workspace/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_file(
    session_id: UUID,
    request: Request,
    file: UploadFile,
    path: str = Query(
        "",
        description="Relative directory within the workspace to place the file. Empty = root.",
    ),
    tenant: TenantContext = Depends(get_current_tenant),
) -> UploadResponse:
    """Upload a file into the session's workspace."""
    _require_service_account_api_route(request, tenant)
    store = _get_session_store(request)
    storage = _get_storage(request)
    bucket = await _get_storage_bucket(store, session_id, tenant)

    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided.")

    safe_name = PurePosixPath(file.filename).name
    if not safe_name or safe_name in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid filename.")

    key = f"{path}/{safe_name}" if path else safe_name
    _validate_path(key)

    contents = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(contents) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds maximum upload size ({_MAX_UPLOAD_BYTES // 1_000_000} MB).",
        )

    await storage.write(bucket, session_workspace_key(session_id, key), contents)

    return UploadResponse(path=key, size=len(contents))


@router.get("/api/sessions/{session_id}/workspace/download")
@router.get("/sessions/{session_id}/workspace/download")
async def download_file(
    session_id: UUID,
    request: Request,
    path: str = Query(..., description="Relative path within the workspace"),
    tenant: TenantContext = Depends(get_current_tenant),
) -> Response:
    """Download a file from the session's workspace."""
    _require_service_account_api_route(request, tenant)
    _validate_path(path)
    store = _get_session_store(request)
    storage = _get_storage(request)
    bucket = await _get_storage_bucket(store, session_id, tenant)

    storage_key = session_workspace_key(session_id, path)
    if not await storage.exists(bucket, storage_key):
        raise HTTPException(status_code=404, detail=f"File not found: {path}")

    try:
        info = await storage.stat(bucket, storage_key)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"File not found: {path}")

    if info["size"] > _MAX_DOWNLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File too large to download.",
        )

    data = await storage.read(bucket, storage_key)
    mime, _ = mimetypes.guess_type(path)
    filename = PurePosixPath(path).name

    return Response(
        content=data,
        media_type=mime or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete(
    "/api/sessions/{session_id}/workspace/file",
    response_model=DeleteResponse,
)
@router.delete(
    "/sessions/{session_id}/workspace/file",
    response_model=DeleteResponse,
)
async def delete_file(
    session_id: UUID,
    request: Request,
    path: str = Query(..., description="Relative path within the workspace"),
    tenant: TenantContext = Depends(get_current_tenant),
) -> DeleteResponse:
    """Delete a file from the session's workspace."""
    _require_service_account_api_route(request, tenant)
    _validate_path(path)
    store = _get_session_store(request)
    storage = _get_storage(request)
    bucket = await _get_storage_bucket(store, session_id, tenant)

    storage_key = session_workspace_key(session_id, path)
    if not await storage.exists(bucket, storage_key):
        raise HTTPException(status_code=404, detail=f"File not found: {path}")

    await storage.delete(bucket, storage_key)

    return DeleteResponse(path=path)
