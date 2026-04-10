"""Workspace file browsing for sessions.

Exposes the session's workspace directory as a read-only file tree so
the web UI can display a workspace panel alongside the chat thread.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from surogates.session.store import SessionNotFoundError, SessionStore
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

_MAX_LIST_DEPTH = 8
_MAX_ENTRIES = 500
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

# Directories to skip when walking.
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
    """Content of a single workspace file."""

    path: str
    content: str
    size: int
    mime_type: str | None = None
    truncated: bool = False


class Checkpoint(BaseModel):
    """A single workspace checkpoint."""

    hash: str
    short_hash: str
    timestamp: str
    reason: str
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0


class CheckpointListResponse(BaseModel):
    """Available checkpoints for a session's workspace."""

    checkpoints: list[Checkpoint]


class RollbackRequest(BaseModel):
    """Request to restore workspace to a checkpoint."""

    checkpoint_hash: str
    file_path: str | None = None


class RollbackResponse(BaseModel):
    """Result of a rollback operation."""

    success: bool
    restored_to: str | None = None
    reason: str | None = None
    error: str | None = None


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


async def _get_workspace_path(
    store: SessionStore, session_id: UUID, tenant: TenantContext,
) -> Path:
    """Resolve and validate the workspace path for a session."""
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

    workspace_path = session.config.get("workspace_path")
    if not workspace_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session has no workspace path configured.",
        )

    resolved = Path(workspace_path).resolve()
    if not resolved.is_dir():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace directory does not exist.",
        )

    return resolved


def _is_text_file(path: Path) -> bool:
    """Heuristic check whether a file is likely text-viewable."""
    if path.name in _TEXT_NAMES:
        return True
    ext = path.suffix.lower()
    if ext in _TEXT_EXTENSIONS:
        return True
    mime, _ = mimetypes.guess_type(str(path))
    if mime and mime.startswith("text/"):
        return True
    return False


def _safe_resolve(workspace_root: Path, relative: str) -> Path:
    """Resolve a relative path within the workspace, preventing traversal.

    Uses ``Path.is_relative_to`` for proper path-component comparison,
    avoiding prefix-confusion attacks (e.g. /data/org-acme matching
    /data/org-acme-other).
    """
    target = (workspace_root / relative).resolve()
    if not target.is_relative_to(workspace_root):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Path traversal not allowed.",
        )
    return target


def _walk_tree(root: Path, rel_prefix: str, depth: int, counter: list[int]) -> list[FileEntry]:
    """Recursively walk directory, building FileEntry tree."""
    if depth > _MAX_LIST_DEPTH:
        return []

    entries: list[FileEntry] = []

    try:
        items = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        return []

    for item in items:
        if counter[0] >= _MAX_ENTRIES:
            return entries

        # Skip symlinks to prevent escaping the workspace root.
        if item.is_symlink():
            continue

        name = item.name
        rel_path = f"{rel_prefix}/{name}" if rel_prefix else name

        # Skip hidden dirs and known noise directories.
        if item.is_dir():
            if name.startswith(".") and name not in (".github", ".vscode"):
                continue
            if name in _SKIP_DIRS:
                continue
            children = _walk_tree(item, rel_path, depth + 1, counter)
            counter[0] += 1
            entries.append(FileEntry(
                name=name,
                path=rel_path,
                kind="dir",
                children=children,
            ))
        elif item.is_file():
            try:
                st = item.stat()
            except OSError:
                continue
            # Skip very large files from the tree listing.
            if st.st_size > 50_000_000:
                continue
            counter[0] += 1
            entries.append(FileEntry(
                name=name,
                path=rel_path,
                kind="file",
                size=st.st_size,
            ))

    return entries


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
    workspace = await _get_workspace_path(store, session_id, tenant)

    counter = [0]
    entries = await asyncio.to_thread(_walk_tree, workspace, "", 0, counter)
    truncated = counter[0] >= _MAX_ENTRIES

    return WorkspaceTreeResponse(
        root=workspace.name,
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
    store = _get_session_store(request)
    workspace = await _get_workspace_path(store, session_id, tenant)

    target = _safe_resolve(workspace, path)

    if not target.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File not found: {path}",
        )

    if not _is_text_file(target):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Binary files cannot be viewed in the workspace panel.",
        )

    def _read_file() -> tuple[str, int, bool]:
        st = target.stat()
        truncated = st.st_size > _MAX_READ_BYTES
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(_MAX_READ_BYTES)
        return content, st.st_size, truncated

    try:
        content, size, truncated = await asyncio.to_thread(_read_file)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to read file: {exc}",
        )

    mime, _ = mimetypes.guess_type(str(target))

    return FileContentResponse(
        path=path,
        content=content,
        size=size,
        mime_type=mime,
        truncated=truncated,
    )


@router.get(
    "/sessions/{session_id}/workspace/checkpoints",
    response_model=CheckpointListResponse,
)
async def list_checkpoints(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> CheckpointListResponse:
    """List available checkpoints for a session's workspace."""
    store = _get_session_store(request)
    workspace = await _get_workspace_path(store, session_id, tenant)

    from surogates.tools.utils.checkpoint_manager import CheckpointManager

    mgr = CheckpointManager(enabled=True)
    raw = await asyncio.to_thread(mgr.list_checkpoints, str(workspace))

    return CheckpointListResponse(
        checkpoints=[
            Checkpoint(
                hash=cp["hash"],
                short_hash=cp["short_hash"],
                timestamp=cp["timestamp"],
                reason=cp["reason"],
                files_changed=cp.get("files_changed", 0),
                insertions=cp.get("insertions", 0),
                deletions=cp.get("deletions", 0),
            )
            for cp in raw
        ],
    )


@router.post(
    "/sessions/{session_id}/workspace/rollback",
    response_model=RollbackResponse,
)
async def rollback_checkpoint(
    session_id: UUID,
    body: RollbackRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> RollbackResponse:
    """Restore the workspace to a checkpoint state.

    Takes a pre-rollback snapshot automatically so the operation is reversible.
    """
    store = _get_session_store(request)
    workspace = await _get_workspace_path(store, session_id, tenant)

    from surogates.tools.utils.checkpoint_manager import CheckpointManager

    mgr = CheckpointManager(enabled=True)
    result = await asyncio.to_thread(
        mgr.restore, str(workspace), body.checkpoint_hash, body.file_path,
    )

    if not result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.get("error", "Rollback failed"),
        )

    return RollbackResponse(
        success=True,
        restored_to=result.get("restored_to"),
        reason=result.get("reason"),
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
    """Upload a file into the session's workspace.

    The file is written to ``<workspace>/<path>/<filename>``.
    Parent directories are created automatically.
    """
    store = _get_session_store(request)
    workspace = await _get_workspace_path(store, session_id, tenant)

    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No filename provided.",
        )

    # Sanitise filename — strip path separators to prevent traversal via name.
    safe_name = Path(file.filename).name
    if not safe_name or safe_name in (".", ".."):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename.",
        )

    # Build target path and validate it stays inside the workspace.
    rel = f"{path}/{safe_name}" if path else safe_name
    target = _safe_resolve(workspace, rel)

    # Read the upload body with a size limit.
    contents = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(contents) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds maximum upload size ({_MAX_UPLOAD_BYTES // 1_000_000} MB).",
        )

    def _write() -> int:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
        return len(contents)

    size = await asyncio.to_thread(_write)

    # Compute the relative path to return.
    relative = str(target.relative_to(workspace))

    return UploadResponse(path=relative, size=size)


@router.get("/sessions/{session_id}/workspace/download")
async def download_file(
    session_id: UUID,
    request: Request,
    path: str = Query(..., description="Relative path within the workspace"),
    tenant: TenantContext = Depends(get_current_tenant),
) -> FileResponse:
    """Download a file from the session's workspace.

    Returns the raw file with appropriate Content-Disposition header.
    Supports ``?token=`` query parameter for browser-initiated downloads.
    """
    store = _get_session_store(request)
    workspace = await _get_workspace_path(store, session_id, tenant)

    target = _safe_resolve(workspace, path)

    if not target.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File not found: {path}",
        )

    try:
        size = target.stat().st_size
    except OSError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cannot stat file: {path}",
        )

    if size > _MAX_DOWNLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File too large to download.",
        )

    mime, _ = mimetypes.guess_type(str(target))

    return FileResponse(
        path=str(target),
        filename=target.name,
        media_type=mime or "application/octet-stream",
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
    store = _get_session_store(request)
    workspace = await _get_workspace_path(store, session_id, tenant)

    target = _safe_resolve(workspace, path)

    if not target.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File not found: {path}",
        )

    await asyncio.to_thread(target.unlink)

    return DeleteResponse(path=path)
