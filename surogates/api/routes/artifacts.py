"""Artifacts REST API — session-scoped read + create endpoints.

Artifacts are authored by the LLM via the ``create_artifact`` tool and
listed/fetched by the chat UI.  Payloads never travel on the event log;
the UI loads them on-demand through these routes.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ValidationError

from surogates.artifacts.models import (
    ArtifactKind,
    ArtifactMeta,
    ArtifactSpec,
)
from surogates.artifacts.store import (
    ArtifactLimitError,
    ArtifactNotFoundError,
    ArtifactStore,
)
from surogates.session.events import EventType
from surogates.session.store import SessionNotFoundError, SessionStore
from surogates.storage.backend import StorageBackend
from surogates.storage.tenant import session_workspace_prefix
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Response schemas (request body uses :class:`ArtifactSpec` directly)
# ---------------------------------------------------------------------------


class ArtifactListResponse(BaseModel):
    artifacts: list[ArtifactMeta]


class ArtifactPayloadResponse(BaseModel):
    """Full artifact: metadata plus the kind-specific spec dict."""

    meta: ArtifactMeta
    kind: ArtifactKind
    spec: dict[str, Any]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_session_store(request: Request) -> SessionStore:
    store: SessionStore | None = getattr(request.app.state, "session_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session store not available.",
        )
    return store


def _get_storage(request: Request) -> StorageBackend:
    return request.app.state.storage


async def _resolve_storage_bucket(
    store: SessionStore,
    session_id: UUID,
    tenant: TenantContext,
) -> str:
    """Fetch the session, verify tenant access, return its agent bucket."""
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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/sessions/{session_id}/artifacts",
    response_model=ArtifactListResponse,
)
async def list_artifacts(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> ArtifactListResponse:
    """List every artifact that belongs to the session, oldest first."""
    store = _get_session_store(request)
    bucket = await _resolve_storage_bucket(store, session_id, tenant)
    artifact_store = ArtifactStore(
        _get_storage(request),
        session_id=session_id,
        bucket=bucket,
        key_prefix=session_workspace_prefix(session_id),
    )
    artifacts = await artifact_store.list()
    return ArtifactListResponse(artifacts=artifacts)


@router.get(
    "/sessions/{session_id}/artifacts/{artifact_id}",
    response_model=ArtifactPayloadResponse,
)
async def get_artifact(
    session_id: UUID,
    artifact_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> ArtifactPayloadResponse:
    """Fetch a single artifact's metadata and full payload."""
    store = _get_session_store(request)
    bucket = await _resolve_storage_bucket(store, session_id, tenant)
    artifact_store = ArtifactStore(
        _get_storage(request),
        session_id=session_id,
        bucket=bucket,
        key_prefix=session_workspace_prefix(session_id),
    )
    try:
        meta = await artifact_store.get_meta(artifact_id)
        payload = await artifact_store.get_payload(
            artifact_id, version=meta.version,
        )
    except ArtifactNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Artifact {artifact_id} not found.",
        )
    return ArtifactPayloadResponse(
        meta=meta,
        kind=ArtifactKind(payload["kind"]),
        spec=payload["spec"],
    )


@router.post(
    "/sessions/{session_id}/artifacts",
    response_model=ArtifactMeta,
    status_code=status.HTTP_201_CREATED,
)
async def create_artifact(
    session_id: UUID,
    body: ArtifactSpec,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> ArtifactMeta:
    """Create a new artifact and emit an ``artifact.created`` event.

    The event carries only metadata; the spec stays in the session
    bucket and is fetched by the UI via :func:`get_artifact`.
    """
    store = _get_session_store(request)
    bucket = await _resolve_storage_bucket(store, session_id, tenant)

    try:
        body.validate_spec()
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(),
        )

    artifact_store = ArtifactStore(
        _get_storage(request),
        session_id=session_id,
        bucket=bucket,
        key_prefix=session_workspace_prefix(session_id),
    )
    try:
        meta = await artifact_store.create(
            name=body.name, kind=body.kind, spec=body.spec,
        )
    except ArtifactLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(exc),
        )

    await store.emit_event(
        session_id,
        EventType.ARTIFACT_CREATED,
        {
            "artifact_id": str(meta.artifact_id),
            "name": meta.name,
            "kind": meta.kind.value,
            "version": meta.version,
            "size": meta.size,
        },
    )

    return meta
