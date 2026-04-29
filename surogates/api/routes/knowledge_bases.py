"""Management API for knowledge bases.

Step 4 scope: minimal CRUD for ``kb`` and ``kb_source`` rows plus a
synchronous sync trigger. Just enough to drive the ingest pipeline
from ``curl`` end-to-end while the management UI lands later.

All endpoints are tenant-scoped via the existing ``get_current_tenant``
dependency. KB writes set ``app.org_id`` on the connection so the RLS
policies in ``surogates/db/kb.sql`` engage as a backstop.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import text

from surogates.jobs.kb_ingest import IngestLocked, run_ingest
from surogates.jobs.kb_sources._base import IngestResult
from surogates.jobs.kb_sources.file_upload import holding_prefix_for
from surogates.jobs.wiki_compile import CompileResult, compile_wiki_for_kb
from surogates.storage.kb_storage import KbStorage
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class KbCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    agents_md: str = ""
    embedding_model: str = "mxbai-embed-large"
    embedding_dim: int = 1024


class KbOut(BaseModel):
    id: UUID
    org_id: Optional[UUID]
    name: str
    description: Optional[str]
    is_platform: bool
    status: str
    embedding_model: str
    embedding_dim: int
    last_compiled_at: Optional[datetime]
    created_at: datetime


class KbListOut(BaseModel):
    kbs: list[KbOut]


class SourceCreate(BaseModel):
    kind: str
    config: dict[str, Any] = Field(default_factory=dict)
    schedule: Optional[str] = None


class SourceOut(BaseModel):
    id: UUID
    kb_id: UUID
    kind: str
    config: dict[str, Any]
    schedule: Optional[str]
    last_synced_at: Optional[datetime]
    last_status: Optional[str]
    last_error: Optional[str]


class SourceListOut(BaseModel):
    sources: list[SourceOut]


class SyncOut(BaseModel):
    """Result of a synchronous ingest run."""

    source_id: UUID
    docs_added: int
    docs_updated: int
    docs_unchanged: int
    docs_skipped: int
    bytes_written: int
    total: int


class UploadedFileInfo(BaseModel):
    filename: str
    size: int


class UploadOut(BaseModel):
    source_id: UUID
    files: list[UploadedFileInfo]


class RecompileOut(BaseModel):
    """Result of a wiki-compile pass."""

    kb_id: UUID
    entries_added: int
    entries_updated: int
    entries_unchanged: int
    chunks_added: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _set_org_guc(db: Any, org_id: UUID) -> None:
    """Set ``app.org_id`` on the connection so RLS policies engage.

    The application-layer filter remains the primary tenancy
    enforcement; this is the suspenders layer documented in
    ``kb.sql``.
    """
    await db.execute(
        text("SELECT set_config('app.org_id', :v, true)"),
        {"v": str(org_id)},
    )


def _row_to_kb_out(row: Any) -> KbOut:
    return KbOut(
        id=row.id,
        org_id=row.org_id,
        name=row.name,
        description=row.description,
        is_platform=row.is_platform,
        status=row.status,
        embedding_model=row.embedding_model,
        embedding_dim=row.embedding_dim,
        last_compiled_at=row.last_compiled_at,
        created_at=row.created_at,
    )


def _row_to_source_out(row: Any) -> SourceOut:
    config = row.config
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except json.JSONDecodeError:
            config = {}
    return SourceOut(
        id=row.id,
        kb_id=row.kb_id,
        kind=row.kind,
        config=config or {},
        schedule=row.schedule,
        last_synced_at=row.last_synced_at,
        last_status=row.last_status,
        last_error=row.last_error,
    )


# ---------------------------------------------------------------------------
# KB CRUD
# ---------------------------------------------------------------------------


@router.post(
    "/kb",
    response_model=KbOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_kb(
    body: KbCreate,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> KbOut:
    """Create a new KB owned by the calling tenant.

    Platform KBs (org_id IS NULL) are NOT created via this endpoint —
    they're seeded by privileged migration code.
    """
    factory = request.app.state.session_factory
    kb_id = uuid.uuid4()
    async with factory() as db:
        await _set_org_guc(db, tenant.org_id)
        try:
            await db.execute(
                text(
                    "INSERT INTO kb "
                    "(id, org_id, name, description, agents_md, "
                    " embedding_model, embedding_dim, is_platform) "
                    "VALUES (:id, :org_id, :name, :description, :agents_md, "
                    "        :embedding_model, :embedding_dim, false)"
                ),
                {
                    "id": kb_id,
                    "org_id": tenant.org_id,
                    "name": body.name,
                    "description": body.description,
                    "agents_md": body.agents_md,
                    "embedding_model": body.embedding_model,
                    "embedding_dim": body.embedding_dim,
                },
            )
            await db.commit()
        except Exception as exc:
            logger.warning("kb create failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            )

        row = (
            await db.execute(
                text("SELECT * FROM kb WHERE id = :id"),
                {"id": kb_id},
            )
        ).first()
    if row is None:
        raise HTTPException(status_code=500, detail="kb create round-trip failed")
    return _row_to_kb_out(row)


@router.get("/kb", response_model=KbListOut)
async def list_kbs(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> KbListOut:
    """List KBs visible to the calling tenant (own org + platform)."""
    factory = request.app.state.session_factory
    async with factory() as db:
        await _set_org_guc(db, tenant.org_id)
        rows = (
            await db.execute(
                text(
                    "SELECT * FROM kb "
                    "WHERE org_id IS NULL OR org_id = :org_id "
                    "ORDER BY is_platform DESC, name ASC"
                ),
                {"org_id": tenant.org_id},
            )
        ).all()
    return KbListOut(kbs=[_row_to_kb_out(r) for r in rows])


@router.get("/kb/{kb_id}", response_model=KbOut)
async def get_kb(
    kb_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> KbOut:
    factory = request.app.state.session_factory
    async with factory() as db:
        await _set_org_guc(db, tenant.org_id)
        row = (
            await db.execute(
                text(
                    "SELECT * FROM kb "
                    "WHERE id = :id "
                    "  AND (org_id IS NULL OR org_id = :org_id)"
                ),
                {"id": kb_id, "org_id": tenant.org_id},
            )
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="kb not found")
    return _row_to_kb_out(row)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


@router.post(
    "/kb/{kb_id}/sources",
    response_model=SourceOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_source(
    kb_id: UUID,
    body: SourceCreate,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> SourceOut:
    """Add a source to a KB.

    The KB must be owned by the calling tenant — adding sources to a
    platform KB requires the privileged migration role (and bypasses
    RLS).
    """
    factory = request.app.state.session_factory
    async with factory() as db:
        await _set_org_guc(db, tenant.org_id)
        # Confirm the kb exists AND is owned by this tenant. RLS on
        # kb_source's WITH CHECK would also block a cross-tenant insert
        # but we surface a clear 404 here too.
        kb_row = (
            await db.execute(
                text(
                    "SELECT id FROM kb "
                    "WHERE id = :id AND org_id = :org_id"
                ),
                {"id": kb_id, "org_id": tenant.org_id},
            )
        ).first()
        if kb_row is None:
            raise HTTPException(status_code=404, detail="kb not found")

        source_id = uuid.uuid4()
        await db.execute(
            text(
                "INSERT INTO kb_source "
                "(id, kb_id, kind, config, schedule) "
                "VALUES (:id, :kb_id, :kind, :config, :schedule)"
            ),
            {
                "id": source_id,
                "kb_id": kb_id,
                "kind": body.kind,
                "config": json.dumps(body.config),
                "schedule": body.schedule,
            },
        )
        await db.commit()

        row = (
            await db.execute(
                text("SELECT * FROM kb_source WHERE id = :id"),
                {"id": source_id},
            )
        ).first()
    return _row_to_source_out(row)


@router.get(
    "/kb/{kb_id}/sources",
    response_model=SourceListOut,
)
async def list_sources(
    kb_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> SourceListOut:
    factory = request.app.state.session_factory
    async with factory() as db:
        await _set_org_guc(db, tenant.org_id)
        rows = (
            await db.execute(
                text(
                    "SELECT s.* FROM kb_source s "
                    "JOIN kb ON kb.id = s.kb_id "
                    "WHERE s.kb_id = :kb_id "
                    "  AND s.deleted_at IS NULL "
                    "  AND (kb.org_id IS NULL OR kb.org_id = :org_id) "
                    "ORDER BY s.created_at DESC"
                ),
                {"kb_id": kb_id, "org_id": tenant.org_id},
            )
        ).all()
    return SourceListOut(sources=[_row_to_source_out(r) for r in rows])


@router.post(
    "/kb/{kb_id}/sources/{source_id}/sync",
    response_model=SyncOut,
)
async def sync_source(
    kb_id: UUID,
    source_id: UUID,
    request: Request,
    compile: bool = False,
    tenant: TenantContext = Depends(get_current_tenant),
) -> SyncOut:
    """Trigger a synchronous ingest of *source_id*.

    Holds the per-source advisory lock; concurrent calls on the same
    source return 409 Conflict. The ingest runs in-request, so the
    HTTP timeout is the bound on how long we'll wait — long-running
    ingests should be moved to a background queue (later step).

    Pass ``?compile=true`` to chain a wiki-compile pass after a
    successful sync (writes wiki_entry rows + chunks + embeddings so
    the new content shows up in ``kb_search`` immediately). Otherwise
    compile must be triggered separately via
    ``POST /v1/kb/{id}/recompile``.
    """
    factory = request.app.state.session_factory
    storage_backend = request.app.state.storage

    # Confirm the source belongs to a KB owned by this tenant.
    async with factory() as db:
        await _set_org_guc(db, tenant.org_id)
        row = (
            await db.execute(
                text(
                    "SELECT s.id FROM kb_source s "
                    "JOIN kb ON kb.id = s.kb_id "
                    "WHERE s.id = :id AND s.kb_id = :kb_id "
                    "  AND kb.org_id = :org_id"
                ),
                {"id": source_id, "kb_id": kb_id, "org_id": tenant.org_id},
            )
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="source not found")

    try:
        result: IngestResult = await run_ingest(
            source_id,
            session_factory=factory,
            storage_backend=storage_backend,
            block=False,
        )
    except IngestLocked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="another sync of this source is already in progress",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Optional chained compile. We pull the embedder from app.state so
    # vector embeddings populate when the platform has an embedding
    # service configured; without one, chunks land with NULL embedding
    # and kb_search runs BM25-only.
    if compile:
        embedder = getattr(request.app.state, "embedder", None)
        try:
            await compile_wiki_for_kb(
                kb_id,
                session_factory=factory,
                storage_backend=storage_backend,
                embedder=embedder,
                only_changed_since=True,
            )
        except Exception as exc:
            logger.exception("post-sync compile failed for kb=%s", kb_id)
            # Don't fail the whole request — the ingest succeeded; the
            # compile failure is recoverable via /recompile.
            raise HTTPException(
                status_code=status.HTTP_207_MULTI_STATUS,
                detail=(
                    "ingest succeeded but compile failed: "
                    f"{exc!s}. Retry via POST /v1/kb/{{id}}/recompile."
                ),
            )

    return SyncOut(
        source_id=source_id,
        docs_added=result.docs_added,
        docs_updated=result.docs_updated,
        docs_unchanged=result.docs_unchanged,
        docs_skipped=result.docs_skipped,
        bytes_written=result.bytes_written,
        total=result.total,
    )


@router.post(
    "/kb/{kb_id}/recompile",
    response_model=RecompileOut,
)
async def recompile_kb(
    kb_id: UUID,
    request: Request,
    full: bool = False,
    tenant: TenantContext = Depends(get_current_tenant),
) -> RecompileOut:
    """Run the wiki-compile pass for *kb_id*.

    By default only raw_docs ingested since the KB's
    ``last_compiled_at`` watermark are processed; pass ``?full=true``
    to force a full recompile (used after schema changes or when the
    chunker is updated).

    Embedder is pulled from ``app.state.embedder``; when no embedder
    is configured, chunks are inserted with ``embedding=NULL`` and
    ``kb_search`` falls back to BM25-only.
    """
    factory = request.app.state.session_factory
    storage_backend = request.app.state.storage
    embedder = getattr(request.app.state, "embedder", None)

    async with factory() as db:
        await _set_org_guc(db, tenant.org_id)
        row = (
            await db.execute(
                text(
                    "SELECT id FROM kb "
                    "WHERE id = :id AND org_id = :org_id"
                ),
                {"id": kb_id, "org_id": tenant.org_id},
            )
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="kb not found")

    try:
        result: CompileResult = await compile_wiki_for_kb(
            kb_id,
            session_factory=factory,
            storage_backend=storage_backend,
            embedder=embedder,
            only_changed_since=not full,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return RecompileOut(
        kb_id=kb_id,
        entries_added=result.entries_added,
        entries_updated=result.entries_updated,
        entries_unchanged=result.entries_unchanged,
        chunks_added=result.chunks_added,
    )


# ---------------------------------------------------------------------------
# File upload (for the file_upload source kind)
# ---------------------------------------------------------------------------


_FILENAME_BAD = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str) -> str:
    """Strip path separators and unsafe chars from a client-supplied name.

    Defensive: clients can send anything in a multipart filename, and
    we put it directly into a bucket key. Drop the dirname, replace
    everything outside ``[A-Za-z0-9._-]`` with ``-``, refuse empties
    and dotfiles.
    """
    name = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    cleaned = _FILENAME_BAD.sub("-", name).strip("-.")
    return cleaned or "upload.bin"


@router.post(
    "/kb/{kb_id}/sources/{source_id}/files",
    response_model=UploadOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_files(
    kb_id: UUID,
    source_id: UUID,
    request: Request,
    files: list[UploadFile] = File(...),
    tenant: TenantContext = Depends(get_current_tenant),
) -> UploadOut:
    """Upload one or more files to a ``file_upload`` source's holding
    prefix.

    Bytes land at
    ``{kb-bucket}/holding/{source_id}/{safe-filename}``. Trigger the
    sync endpoint afterwards to convert them into ``raw_doc`` rows
    via markitdown. Holding bytes are preserved by default so
    re-syncing converts them again without a re-upload.
    """
    factory = request.app.state.session_factory
    storage_backend = request.app.state.storage

    # Confirm the source belongs to a kb owned by this tenant AND is
    # of kind=file_upload (other kinds wouldn't know what to do with
    # holding files).
    async with factory() as db:
        await _set_org_guc(db, tenant.org_id)
        row = (
            await db.execute(
                text(
                    "SELECT s.id, s.kind, kb.org_id AS kb_org_id, "
                    "       kb.name AS kb_name "
                    "FROM kb_source s "
                    "JOIN kb ON kb.id = s.kb_id "
                    "WHERE s.id = :sid AND s.kb_id = :kbid "
                    "  AND kb.org_id = :org_id"
                ),
                {
                    "sid": source_id,
                    "kbid": kb_id,
                    "org_id": tenant.org_id,
                },
            )
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="source not found")
    if row.kind != "file_upload":
        raise HTTPException(
            status_code=400,
            detail=(
                f"upload supported only for kind='file_upload', "
                f"this source is kind={row.kind!r}"
            ),
        )

    storage = KbStorage(storage_backend)
    holding = holding_prefix_for(source_id)
    uploaded: list[UploadedFileInfo] = []

    for f in files:
        original = f.filename or "upload.bin"
        safe = _safe_filename(original)
        data = await f.read()
        if not data:
            continue
        await storage.write_entry(
            kb_org_id=row.kb_org_id,
            kb_name=row.kb_name,
            path=f"{holding}/{safe}",
            data=data,
        )
        uploaded.append(UploadedFileInfo(filename=safe, size=len(data)))

    if not uploaded:
        raise HTTPException(status_code=400, detail="no files uploaded")

    return UploadOut(source_id=source_id, files=uploaded)
