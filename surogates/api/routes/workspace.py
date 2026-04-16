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
from surogates.storage.tenant import session_bucket
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


async def _get_session_bucket(
    store: SessionStore, session_id: UUID, tenant: TenantContext,
) -> str:
    """Resolve and validate the session bucket for workspace access."""
    try:
        session = await store.get_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )

    if session.org_id != tenant.org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )

    bucket = session.config.get("workspace_bucket")
    if not bucket:
        # Fallback for sessions created before bucket support.
        bucket = session_bucket(session_id)

    return bucket


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
    """Reject path traversal attempts."""
    parts = PurePosixPath(path).parts
    if ".." in parts:
        raise HTTPException(status_code=403, detail="Path traversal not allowed.")
    if path.startswith("/"):
        raise HTTPException(status_code=403, detail="Absolute paths not allowed.")


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
    "/sessions/{session_id}/workspace/tree",
    response_model=WorkspaceTreeResponse,
)
async def get_workspace_tree(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> WorkspaceTreeResponse:
    """Return the recursive file tree for a session's workspace."""
    store = _get_session_store(request)
    storage = _get_storage(request)
    bucket = await _get_session_bucket(store, session_id, tenant)

    keys = await storage.list_keys(bucket)
    entries = _build_tree(keys)
    truncated = len(keys) >= _MAX_ENTRIES

    return WorkspaceTreeResponse(
        root=bucket,
        entries=entries,
        truncated=truncated,
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
    _validate_path(path)
    store = _get_session_store(request)
    storage = _get_storage(request)
    bucket = await _get_session_bucket(store, session_id, tenant)

    is_text = _is_text_key(path)
    is_image = _is_image_key(path)

    if not is_text and not is_image:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Binary files cannot be viewed in the workspace panel.",
        )

    try:
        data = await storage.read(bucket, path)
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
    store = _get_session_store(request)
    storage = _get_storage(request)
    bucket = await _get_session_bucket(store, session_id, tenant)

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

    await storage.write(bucket, key, contents)

    return UploadResponse(path=key, size=len(contents))


@router.get("/sessions/{session_id}/workspace/download")
async def download_file(
    session_id: UUID,
    request: Request,
    path: str = Query(..., description="Relative path within the workspace"),
    tenant: TenantContext = Depends(get_current_tenant),
) -> Response:
    """Download a file from the session's workspace."""
    _validate_path(path)
    store = _get_session_store(request)
    storage = _get_storage(request)
    bucket = await _get_session_bucket(store, session_id, tenant)

    if not await storage.exists(bucket, path):
        raise HTTPException(status_code=404, detail=f"File not found: {path}")

    try:
        info = await storage.stat(bucket, path)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"File not found: {path}")

    if info["size"] > _MAX_DOWNLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File too large to download.",
        )

    data = await storage.read(bucket, path)
    mime, _ = mimetypes.guess_type(path)
    filename = PurePosixPath(path).name

    return Response(
        content=data,
        media_type=mime or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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
    _validate_path(path)
    store = _get_session_store(request)
    storage = _get_storage(request)
    bucket = await _get_session_bucket(store, session_id, tenant)

    if not await storage.exists(bucket, path):
        raise HTTPException(status_code=404, detail=f"File not found: {path}")

    await storage.delete(bucket, path)

    return DeleteResponse(path=path)
