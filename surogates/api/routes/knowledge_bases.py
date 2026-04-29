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


class RawDocOut(BaseModel):
    id: UUID
    path: str
    content_sha: str
    title: Optional[str]
    url: Optional[str]
    ingested_at: datetime


class RawDocListOut(BaseModel):
    kb_id: UUID
    raw_docs: list[RawDocOut]
    total: int


class WikiEntryOut(BaseModel):
    id: UUID
    path: str
    kind: str
    content_sha: str
    sources: list[UUID]
    updated_at: datetime


class WikiEntryListOut(BaseModel):
    kb_id: UUID
    wiki_entries: list[WikiEntryOut]
    total: int


class GrantBody(BaseModel):
    # String identifier — same shape as ``session.agent_id`` (the
    # deployment name or sub-agent type name). NOT a UUID.
    agent_id: str = Field(..., min_length=1, max_length=200)


class GrantOut(BaseModel):
    agent_id: str
    kb_id: UUID
    granted_at: datetime


class GrantListOut(BaseModel):
    kb_id: UUID
    grants: list[GrantOut]


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
# Browse: raw_docs and wiki_entries within a KB
# ---------------------------------------------------------------------------


async def _confirm_kb_visible(
    factory,
    *,
    kb_id: UUID,
    org_id: UUID,
) -> None:
    """Raise 404 unless the KB is owned by *org_id* OR is platform-shared."""
    async with factory() as db:
        await _set_org_guc(db, org_id)
        row = (
            await db.execute(
                text(
                    "SELECT 1 FROM kb "
                    "WHERE id = :id "
                    "  AND (org_id = :org_id OR org_id IS NULL)"
                ),
                {"id": kb_id, "org_id": org_id},
            )
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="kb not found")


@router.get(
    "/kb/{kb_id}/raw",
    response_model=RawDocListOut,
)
async def list_raw_docs(
    kb_id: UUID,
    request: Request,
    limit: int = 100,
    offset: int = 0,
    tenant: TenantContext = Depends(get_current_tenant),
) -> RawDocListOut:
    """List raw_docs ingested into this KB.

    Paginated; ``limit`` capped at 500 to protect the response size.
    Used by the management UI to render the source's recent ingest
    log + by admin tools to sanity-check what's in the KB.
    """
    factory = request.app.state.session_factory
    await _confirm_kb_visible(factory, kb_id=kb_id, org_id=tenant.org_id)

    limit = max(1, min(500, limit))
    offset = max(0, offset)
    async with factory() as db:
        await _set_org_guc(db, tenant.org_id)
        total = (
            await db.execute(
                text(
                    "SELECT count(*) FROM kb_raw_doc WHERE kb_id = :id"
                ),
                {"id": kb_id},
            )
        ).scalar()
        rows = (
            await db.execute(
                text(
                    "SELECT id, path, content_sha, title, url, "
                    "       ingested_at "
                    "FROM kb_raw_doc WHERE kb_id = :id "
                    "ORDER BY ingested_at DESC, path ASC "
                    "LIMIT :limit OFFSET :offset"
                ),
                {"id": kb_id, "limit": limit, "offset": offset},
            )
        ).all()
    return RawDocListOut(
        kb_id=kb_id,
        raw_docs=[
            RawDocOut(
                id=r.id,
                path=r.path,
                content_sha=r.content_sha,
                title=r.title,
                url=r.url,
                ingested_at=r.ingested_at,
            )
            for r in rows
        ],
        total=int(total or 0),
    )


@router.get(
    "/kb/{kb_id}/wiki",
    response_model=WikiEntryListOut,
)
async def list_wiki_entries(
    kb_id: UUID,
    request: Request,
    limit: int = 100,
    offset: int = 0,
    tenant: TenantContext = Depends(get_current_tenant),
) -> WikiEntryListOut:
    """List wiki_entries (compiled retrieval units) for this KB."""
    factory = request.app.state.session_factory
    await _confirm_kb_visible(factory, kb_id=kb_id, org_id=tenant.org_id)

    limit = max(1, min(500, limit))
    offset = max(0, offset)
    async with factory() as db:
        await _set_org_guc(db, tenant.org_id)
        total = (
            await db.execute(
                text(
                    "SELECT count(*) FROM kb_wiki_entry WHERE kb_id = :id"
                ),
                {"id": kb_id},
            )
        ).scalar()
        rows = (
            await db.execute(
                text(
                    "SELECT id, path, kind, content_sha, sources, "
                    "       updated_at "
                    "FROM kb_wiki_entry WHERE kb_id = :id "
                    "ORDER BY updated_at DESC, path ASC "
                    "LIMIT :limit OFFSET :offset"
                ),
                {"id": kb_id, "limit": limit, "offset": offset},
            )
        ).all()
    return WikiEntryListOut(
        kb_id=kb_id,
        wiki_entries=[
            WikiEntryOut(
                id=r.id,
                path=r.path,
                kind=r.kind,
                content_sha=r.content_sha,
                sources=list(r.sources or []),
                updated_at=r.updated_at,
            )
            for r in rows
        ],
        total=int(total or 0),
    )


# ---------------------------------------------------------------------------
# Delete: source (tombstone) + KB (CASCADE)
# ---------------------------------------------------------------------------


@router.delete(
    "/kb/{kb_id}/sources/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_source(
    kb_id: UUID,
    source_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> None:
    """Tombstone a source.

    Sets ``kb_source.deleted_at = NOW()``; does not remove the row or
    its raw_doc children. The wiki maintainer's next pass rewrites
    affected wiki entries to drop references to docs from the
    tombstoned source. Hard-purge is a separate admin operation.
    """
    factory = request.app.state.session_factory
    async with factory() as db:
        await _set_org_guc(db, tenant.org_id)
        result = await db.execute(
            text(
                "UPDATE kb_source SET deleted_at = NOW() "
                "WHERE id = :sid AND kb_id = :kbid "
                "  AND kb_id IN (SELECT id FROM kb WHERE org_id = :org_id) "
                "  AND deleted_at IS NULL"
            ),
            {
                "sid": source_id,
                "kbid": kb_id,
                "org_id": tenant.org_id,
            },
        )
        await db.commit()
    if result.rowcount == 0:
        raise HTTPException(
            status_code=404,
            detail="source not found or already deleted",
        )


@router.delete(
    "/kb/{kb_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_kb(
    kb_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> None:
    """Hard-delete a KB and all its descendants.

    Cascades through kb_source, kb_raw_doc, kb_wiki_entry, kb_chunk
    via FK ``ON DELETE CASCADE``. Garage objects are NOT cleaned up
    here — that's a separate admin sweep keyed off the watermark.
    Platform KBs (``org_id IS NULL``) are not deletable through this
    endpoint; the privileged migration role manages them.
    """
    factory = request.app.state.session_factory
    async with factory() as db:
        await _set_org_guc(db, tenant.org_id)
        result = await db.execute(
            text(
                "DELETE FROM kb "
                "WHERE id = :id AND org_id = :org_id"
            ),
            {"id": kb_id, "org_id": tenant.org_id},
        )
        await db.commit()
    if result.rowcount == 0:
        raise HTTPException(
            status_code=404,
            detail="kb not found",
        )


# ---------------------------------------------------------------------------
# Per-agent grants
# ---------------------------------------------------------------------------


@router.post(
    "/kb/{kb_id}/grants",
    response_model=GrantOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_grant(
    kb_id: UUID,
    body: GrantBody,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> GrantOut:
    """Grant *body.agent_id* read access to *kb_id*.

    ``body.agent_id`` is a string — it must match the value the harness
    will pass as ``session.agent_id`` for sessions running under that
    agent (typically the deployment name like ``testknoledgeagent`` or
    a sub-agent type name). The KB must be owned by the calling
    tenant. We don't FK-check the string against the agents table —
    grants for top-level deployment names that aren't in surogates'
    sub-agent type table are still valid.

    Idempotent — granting twice returns the existing row.
    """
    factory = request.app.state.session_factory
    async with factory() as db:
        await _set_org_guc(db, tenant.org_id)
        # Confirm KB is owned by tenant.
        kb_row = (
            await db.execute(
                text(
                    "SELECT id FROM kb WHERE id = :id AND org_id = :org_id"
                ),
                {"id": kb_id, "org_id": tenant.org_id},
            )
        ).first()
        if kb_row is None:
            raise HTTPException(status_code=404, detail="kb not found")

        await db.execute(
            text(
                "INSERT INTO agent_kb_grant (agent_id, kb_id) "
                "VALUES (:agent_id, :kb_id) "
                "ON CONFLICT DO NOTHING"
            ),
            {"agent_id": body.agent_id, "kb_id": kb_id},
        )
        row = (
            await db.execute(
                text(
                    "SELECT agent_id, kb_id, granted_at "
                    "FROM agent_kb_grant "
                    "WHERE agent_id = :agent_id AND kb_id = :kb_id"
                ),
                {"agent_id": body.agent_id, "kb_id": kb_id},
            )
        ).first()
        await db.commit()
    return GrantOut(
        agent_id=row.agent_id,
        kb_id=row.kb_id,
        granted_at=row.granted_at,
    )


@router.delete(
    "/kb/{kb_id}/grants/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_grant(
    kb_id: UUID,
    agent_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> None:
    """Revoke a specific (agent, kb) grant. 404 if it didn't exist."""
    factory = request.app.state.session_factory
    async with factory() as db:
        await _set_org_guc(db, tenant.org_id)
        result = await db.execute(
            text(
                "DELETE FROM agent_kb_grant g "
                "USING kb "
                "WHERE g.kb_id = kb.id "
                "  AND g.kb_id = :kb_id "
                "  AND g.agent_id = :agent_id "
                "  AND kb.org_id = :org_id"
            ),
            {
                "kb_id": kb_id,
                "agent_id": agent_id,
                "org_id": tenant.org_id,
            },
        )
        await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="grant not found")


@router.get(
    "/kb/{kb_id}/grants",
    response_model=GrantListOut,
)
async def list_grants(
    kb_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> GrantListOut:
    """List all agents granted access to this KB."""
    factory = request.app.state.session_factory
    await _confirm_kb_visible(factory, kb_id=kb_id, org_id=tenant.org_id)

    async with factory() as db:
        await _set_org_guc(db, tenant.org_id)
        rows = (
            await db.execute(
                text(
                    "SELECT agent_id, kb_id, granted_at "
                    "FROM agent_kb_grant WHERE kb_id = :id "
                    "ORDER BY granted_at DESC"
                ),
                {"id": kb_id},
            )
        ).all()
    return GrantListOut(
        kb_id=kb_id,
        grants=[
            GrantOut(
                agent_id=r.agent_id,
                kb_id=r.kb_id,
                granted_at=r.granted_at,
            )
            for r in rows
        ],
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
