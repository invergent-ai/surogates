"""Workspace file browsing for sessions.

Exposes the session's files so the web UI can display a workspace panel
alongside the chat thread: a cloud session's workspace in object storage
(``LocalBackend`` in dev, ``S3Backend`` in production), or the folder on the
user's computer a local-folder chat works on, through that computer
(``surogates.session.files``).
"""

from __future__ import annotations

import asyncio
import base64
import errno
import hashlib
import logging
import mimetypes
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from surogates.api.session_guards import (
    require_device_access,
    require_session_visible,
    require_user_writable_session,
)
from surogates.devices.operations import OperationConflict, TooManyRequests
from surogates.devices.workspace import SHOWN_DOT_FOLDERS, DeviceOperationError, DeviceWorkspaceIO
from surogates.session.files import ComputerAway, DeviceAccess, session_files
from surogates.session.models import Session
from surogates.session.store import SessionNotFoundError, SessionStore
from surogates.storage.backend import StorageBackend
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext
from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError
from surogates.tools.workspace_io import RevisionConflict, WorkspaceFiles


def _workspace_root_id(session: Session) -> str:
    """Return the storage-prefix id for *session*'s workspace.

    New children (post shared-workspace deploy) carry an explicit
    ``sandbox_root_session_id`` stamped by ``create_child_session``.
    Use it so the viewer surfaces the parent's files.

    Older children — including legacy scheduled-runner runs that owned
    their own per-session prefix — do NOT have that cached root.  Fall
    back to ``session.id`` so the viewer keeps showing whatever was
    actually written under their own prefix.  This deliberately
    differs from :func:`sandbox_session_key`, which falls back to
    ``parent_id`` for the in-process sandbox pool.
    """
    root = (session.config or {}).get("sandbox_root_session_id")
    return str(root) if root else str(session.id)

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

# Maximum raw bytes for PDF files served inline for PDF.js previews.
_MAX_PDF_BYTES = 25_000_000  # 25 MB

_PDF_EXTENSIONS = frozenset({".pdf"})

# How long a request waits for the computer a local-folder chat's files are
# on.  A read still unanswered then is cancelled.  A change is kept, since its
# computer's user may still be asked to allow it, and answered 202: the same
# request, sent again, joins it.
READ_WITHIN_S = 60.0
CHANGE_WITHIN_S = 20.0

# Names a change, so that sending it again joins it rather than repeating it.
RequestId = Annotated[str | None, Query(pattern=r"^[A-Za-z0-9_-]{16,64}$")]

# What the user's own input gets wrong, not the computer: a name too long for its filesystem, or one it refuses.
_INPUT_ERRNOS = frozenset({errno.ENAMETOOLONG, errno.EINVAL})


def _change(*parts: str | bytes) -> str:
    """Names what a change does, so that its request id cannot be sent again with another.

    Up to 50 MB of an upload: its caller runs it off the event loop.
    """
    digest = hashlib.sha256()
    for part in parts:
        data = part.encode("utf-8", "surrogatepass") if isinstance(part, str) else part
        # Two updates, not one of the two joined: that would copy the data first.
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


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

# Prefixes hidden from the workspace tree but still readable and writable
# by their owning client.  The whiteboard canvas is the case this exists
# for: the browser client is its sole writer (see the whiteboard design
# doc's "Persistence: single writer"), so blocking access would break the
# feature -- but surfacing a canvas.json in the file browser invites a
# delete that silently destroys the user's ink, which the event-log tail
# cannot rebuild.
#
# A project thread's coding checkouts (``.threads/``) are hidden the same
# way: a clone's thousands of files would count against the tree's limit.
# So is the files' history (``_history/``), which only the platform writes.
_HIDDEN_PREFIXES: tuple[str, ...] = ("_whiteboard/", ".threads/", "_history/")

# The one file under them the panel's own page writes: the whiteboard's canvas.
_CANVAS = "_whiteboard/canvas.json"

# The tree's one rule for what it leaves out, which the computer's walk shares
# so that it never enters them: at any depth a folder of _SKIP_DIRS, and a
# dot-folder other than SHOWN_DOT_FOLDERS (_should_skip_dir); at the top, the
# platform's own folders (_is_hidden).
_TOP_HIDDEN: tuple[str, ...] = tuple(prefix.rstrip("/") for prefix in _RESERVED_PREFIXES + _HIDDEN_PREFIXES)


def _is_reserved(key: str) -> bool:
    """Return True if ``key`` points into a reserved internal prefix."""
    return any(key.startswith(p) for p in _RESERVED_PREFIXES)


def _is_hidden(key: str) -> bool:
    """Return True if ``key`` should be kept out of the workspace tree.

    Reserved prefixes are hidden too: blocked implies invisible.
    """
    return _is_reserved(key) or any(key.startswith(p) for p in _HIDDEN_PREFIXES)


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
    contains the raw text.  For inline binary previews, such as images and
    PDFs, ``encoding`` is ``"base64"`` and ``content`` holds the
    base64-encoded bytes.
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


async def _get_workspace_session_bucket_and_root(
    request: Request, store: SessionStore, session_id: UUID, tenant: TenantContext,
) -> tuple[Session, str, str]:
    """Resolve session, bucket, and workspace-root id for storage access.

    For shared-workspace children (delegations, loop iterations created
    after the shared-workspace deploy), the root id comes from the
    session's cached ``sandbox_root_session_id``.  See
    :func:`_workspace_root_id` for the resolution rule and its
    deliberate divergence from :func:`sandbox_session_key`.
    """
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
    await require_session_visible(request, session)

    bucket = session.config.get("storage_bucket")
    if not bucket:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Session {session_id} has no agent bucket.",
        )
    return session, bucket, _workspace_root_id(session)


def _failure(status_code: int, error: str, message: str) -> HTTPException:
    """A structured refusal: the web client shows its message."""
    return HTTPException(status_code=status_code, detail={"error": error, "message": message})


class StillWaiting(Exception):
    """A change its computer has not answered yet: kept, for the same request to ask again."""


def _waiting(request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"request_id": request_id, "message": "Waiting for the computer this chat's folder is on"},
    )


@asynccontextmanager
async def workspace_files(
    request: Request, session: Session, *, request_id: str | None = None, change: str | None = None,
    access: DeviceAccess | None = None,
) -> AsyncIterator[WorkspaceFiles]:
    """*session*'s files for one request, each failure answered as the file panel's.

    A local-folder chat's computer that is offline, or whose access ended,
    is said at once.  A read it does not answer within READ_WITHIN_S is said
    too, and cancelled.  A *change*, named by :func:`_change`, comes under its
    *request_id*, with the *access* ``require_device_access`` gave the route:
    one not answered within CHANGE_WITHIN_S raises StillWaiting, and stays the
    computer's to do.  A cloud chat's files have no deadline here, as before:
    object storage keeps no request to join.
    """
    on_device = False
    try:
        async with session_files(
            session,
            storage=_get_storage(request),
            session_factory=request.app.state.session_factory,
            redis=request.app.state.redis,
            request_id=request_id,
            change=change,
            access=access,
        ) as files:
            on_device = isinstance(files, DeviceWorkspaceIO)
            if not on_device:
                yield files
                return
            async with asyncio.timeout(CHANGE_WITHIN_S if change is not None else READ_WITHIN_S):
                yield files
    except ComputerAway as away:
        if away.revoked:
            raise _failure(status.HTTP_403_FORBIDDEN, "device_revoked", str(away)) from None
        raise _failure(status.HTTP_503_SERVICE_UNAVAILABLE, "device_offline", str(away)) from None
    except TimeoutError:
        if not on_device:
            raise
        if change is not None:
            raise StillWaiting from None
        raise _failure(
            status.HTTP_504_GATEWAY_TIMEOUT, "device_timeout",
            "The computer this chat's folder is on did not answer in time. Try again.",
        ) from None
    except TooManyRequests as exc:
        raise _failure(status.HTTP_429_TOO_MANY_REQUESTS, "device_busy", str(exc)) from None
    except WorkspaceSandboxError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from None
    except RevisionConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except OSError as exc:
        # As the computer worded it: a denial in its user's prompt, a path
        # through a file, a name too long, a file over its cap.
        said = exc.strerror or str(exc)
        if isinstance(exc, PermissionError):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=said) from None
        if isinstance(exc, (FileNotFoundError, NotADirectoryError, IsADirectoryError)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=said) from None
        if isinstance(exc, FileExistsError):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=said) from None
        if exc.errno in _INPUT_ERRNOS:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=said) from None
        if exc.errno == errno.EFBIG:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=said) from None
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=said) from None
    except OperationConflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This request id was sent before with another change. Send the change under a new one.",
        ) from None
    except DeviceOperationError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from None
    except ValueError as exc:
        # A path the computer, or the journal, cannot take: a NUL, a lone surrogate.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None


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


def _is_pdf_key(key: str) -> bool:
    """Heuristic: is this key a PDF file we can display inline?"""
    ext = PurePosixPath(key).suffix.lower()
    return ext in _PDF_EXTENSIONS


def _should_skip_dir(dirname: str) -> bool:
    """Should this directory be skipped in the tree listing?"""
    if dirname.startswith(".") and dirname not in SHOWN_DOT_FOLDERS:
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


def _validate_change(path: str) -> None:
    """Refuse a change under the platform's own folders, which the panel hides, but to the canvas.

    Judged as the computer resolves it, so "./_history/x" is under _history too.
    """
    normal = PurePosixPath(path).as_posix()
    if _is_hidden(normal) and normal != _CANVAS:
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
    session, bucket, _root_id = await _get_workspace_session_bucket_and_root(
        request, store, session_id, tenant,
    )
    await require_device_access(request, session, tenant)

    async with workspace_files(request, session) as files:
        walked = await files.walk(await files.resolve(""), skip=_SKIP_DIRS, skip_top=_TOP_HIDDEN, skip_hidden=True)
    # Drop keys living under reserved prefixes (artifact storage) so
    # internal server-side files don't leak into the workspace browser.
    visible_keys = [path for path, _size in walked.files if not _is_hidden(path)]
    entries = _build_tree(visible_keys)
    truncated = walked.truncated or len(visible_keys) >= _MAX_ENTRIES

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
    session, _bucket, _root_id = await _get_workspace_session_bucket_and_root(
        request, store, session_id, tenant,
    )
    await require_device_access(request, session, tenant)

    is_text = _is_text_key(path)
    is_image = _is_image_key(path)
    is_pdf = _is_pdf_key(path)

    if not is_text and not is_image and not is_pdf:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Binary files cannot be viewed in the workspace panel.",
        )

    mime, _ = mimetypes.guess_type(path)
    preview_bytes = _MAX_PDF_BYTES if is_pdf else _MAX_IMAGE_BYTES if is_image else _MAX_READ_BYTES
    async with workspace_files(request, session) as files:
        key = await files.resolve(path)
        if isinstance(files, DeviceWorkspaceIO):
            # Its size first, so a large file never crosses for a preview.
            st = await files.stat(key)
            if st is None or st.is_dir:
                raise HTTPException(status_code=404, detail=f"File not found: {path}")
            size = st.size
            # A preview too large is refused below, unread; a text file is read only as far as it is shown.
            too_large = (is_image or is_pdf) and size > preview_bytes
            data = b"" if too_large else await files.read(key, max_bytes=preview_bytes)
        else:
            # As before: one read of the whole object, its size its length.
            try:
                data = await files.read(key)
            except FileNotFoundError:
                raise HTTPException(status_code=404, detail=f"File not found: {path}") from None
            size = len(data)

    if is_image or is_pdf:
        kind = "PDF" if is_pdf else "Image"
        if size > preview_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"{kind} too large to preview ({size} bytes, limit {preview_bytes}).",
            )
        return FileContentResponse(
            path=path,
            content=base64.b64encode(data).decode("ascii"),
            size=size,
            mime_type=mime or ("application/pdf" if is_pdf else "application/octet-stream"),
            encoding="base64",
            truncated=False,
        )

    return FileContentResponse(
        path=path,
        content=data[:_MAX_READ_BYTES].decode("utf-8", errors="replace"),
        size=size,
        mime_type=mime,
        encoding="utf-8",
        truncated=size > _MAX_READ_BYTES,
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
    request_id: RequestId = None,
    tenant: TenantContext = Depends(get_current_tenant),
) -> UploadResponse | JSONResponse:
    """Upload a file into the session's workspace.

    202 with its request id when the computer a local-folder chat's folder is
    on has not answered yet: the same upload, sent again with that id, joins it.
    """
    _require_service_account_api_route(request, tenant)
    store = _get_session_store(request)
    session, _bucket, _root_id = await _get_workspace_session_bucket_and_root(
        request, store, session_id, tenant
    )
    require_user_writable_session(session)
    access = await require_device_access(request, session, tenant)

    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided.")

    safe_name = PurePosixPath(file.filename).name
    if not safe_name or safe_name in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid filename.")

    key = f"{path}/{safe_name}" if path else safe_name
    _validate_path(key)
    _validate_change(key)

    contents = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(contents) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds maximum upload size ({_MAX_UPLOAD_BYTES // 1_000_000} MB).",
        )

    request_id = request_id or uuid4().hex
    change = await asyncio.to_thread(_change, "upload", key, contents)
    try:
        async with workspace_files(request, session, request_id=request_id, change=change, access=access) as files:
            await files.write(await files.resolve(key), contents)
    except StillWaiting:
        return _waiting(request_id)

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
    session, _bucket, _root_id = await _get_workspace_session_bucket_and_root(
        request, store, session_id, tenant,
    )
    await require_device_access(request, session, tenant)

    async with workspace_files(request, session) as files:
        key = await files.resolve(path)
        st = await files.stat(key)
        if st is None or st.is_dir:
            raise HTTPException(status_code=404, detail=f"File not found: {path}")
        if st.size > _MAX_DOWNLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="File too large to download.",
            )
        data = await files.read(key)
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
    request_id: RequestId = None,
    tenant: TenantContext = Depends(get_current_tenant),
) -> DeleteResponse | JSONResponse:
    """Delete a file from the session's workspace; 202 as an upload is."""
    _require_service_account_api_route(request, tenant)
    _validate_path(path)
    _validate_change(path)
    store = _get_session_store(request)
    session, _bucket, _root_id = await _get_workspace_session_bucket_and_root(
        request, store, session_id, tenant
    )
    require_user_writable_session(session)
    access = await require_device_access(request, session, tenant)

    request_id = request_id or uuid4().hex
    try:
        async with workspace_files(
            request, session, request_id=request_id, change=_change("delete", path), access=access,
        ) as files:
            try:
                await files.delete(await files.resolve(path))
            except FileNotFoundError:
                raise HTTPException(status_code=404, detail=f"File not found: {path}") from None
    except StillWaiting:
        return _waiting(request_id)

    return DeleteResponse(path=path)
