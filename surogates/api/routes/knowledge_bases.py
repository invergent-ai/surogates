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
import uuid
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from surogates.jobs.kb_ingest import IngestLocked, run_ingest
from surogates.jobs.kb_sources._base import IngestResult
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
    tenant: TenantContext = Depends(get_current_tenant),
) -> SyncOut:
    """Trigger a synchronous ingest of *source_id*.

    Holds the per-source advisory lock; concurrent calls on the same
    source return 409 Conflict. The ingest runs in-request, so the
    HTTP timeout is the bound on how long we'll wait — long-running
    ingests should be moved to a background queue (later step).
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

    return SyncOut(
        source_id=source_id,
        docs_added=result.docs_added,
        docs_updated=result.docs_updated,
        docs_unchanged=result.docs_unchanged,
        docs_skipped=result.docs_skipped,
        bytes_written=result.bytes_written,
        total=result.total,
    )
