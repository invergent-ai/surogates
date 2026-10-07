"""Artifacts REST API — session-scoped read + create endpoints.

Artifacts are authored by the LLM via the ``create_artifact`` tool and
listed/fetched by the chat UI.  Payloads never travel on the event log;
the UI loads them on-demand through these routes.  A local-folder chat's
are read from its folder, by its own user only; its agent makes them in the
folder itself (``surogates.artifacts.store.FolderArtifacts``), so the two
routes that make one refuse such a chat.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
    artifact_event,
)
from surogates.api.routes.workspace import workspace_files
from surogates.devices.binding import device_of
from surogates.sandbox.pool import sandbox_session_key
from surogates.session.files import session_files
from surogates.session.events import EventType
from surogates.api.session_guards import require_device_access, require_session_visible
from surogates.session.store import SessionNotFoundError, SessionStore
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


async def _resolve_session(
    request: Request,
    store: SessionStore,
    session_id: UUID,
    tenant: TenantContext,
) -> Any:
    """Fetch the session and verify tenant access."""
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
    return session


@asynccontextmanager
async def _artifact_files(request: Request, session: Any) -> AsyncIterator[Any]:
    """A chat's files for these routes: a local folder's as the file panel reaches
    them, its answers included; a cloud chat's workspace with its errors as they
    always were."""
    if device_of(session.config) is not None:
        async with workspace_files(request, session) as files:
            yield files
        return
    async with session_files(
        session,
        storage=request.app.state.storage,
        session_factory=request.app.state.session_factory,
        redis=request.app.state.redis,
    ) as files:
        yield files


def _store(files: Any, session: Any) -> ArtifactStore:
    return ArtifactStore(files, session_id=session.id, root=sandbox_session_key(session))


def _refuse_local_folder(session: Any) -> None:
    """409 for a chat on a local folder: its agent makes its artifacts in the folder, under its tool call."""
    if device_of(session.config) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "local_folder",
                "message": "A chat on a local folder has its artifacts made by its agent, in its folder",
            },
        )


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
    session = await _resolve_session(request, store, session_id, tenant)
    await require_device_access(request, session, tenant)
    async with _artifact_files(request, session) as files:
        artifacts = await _store(files, session).list()
    return ArtifactListResponse(artifacts=artifacts)


@router.get(
    "/api/sessions/{session_id}/artifacts/{artifact_id}",
    response_model=ArtifactPayloadResponse,
)
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
    _require_service_account_api_route(request, tenant)
    store = _get_session_store(request)
    session = await _resolve_session(request, store, session_id, tenant)
    await require_device_access(request, session, tenant)
    try:
        async with _artifact_files(request, session) as files:
            artifact_store = _store(files, session)
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
    session = await _resolve_session(request, store, session_id, tenant)
    await require_device_access(request, session, tenant)
    _refuse_local_folder(session)

    try:
        body.validate_spec()
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(),
        )

    try:
        async with _artifact_files(request, session) as files:
            meta = await _store(files, session).create(
                name=body.name, kind=body.kind, spec=body.spec,
            )
    except ArtifactLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(exc),
        )

    await store.emit_event(session_id, EventType.ARTIFACT_CREATED, artifact_event(meta))
    return meta


@router.put(
    "/sessions/{session_id}/artifacts/{artifact_id}",
    response_model=ArtifactMeta,
)
async def update_artifact(
    session_id: UUID,
    artifact_id: UUID,
    body: ArtifactSpec,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> ArtifactMeta:
    """Replace an artifact's payload and emit ``artifact.updated``.

    A revision is a full replacement, so the body is the same
    :class:`ArtifactSpec` a create takes. The event mirrors
    ``artifact.created`` because the UI reducer already treats the two
    identically -- it re-resolves the card from ``artifact_id``, so the
    updated version supersedes the old rendering without a second card.
    """
    store = _get_session_store(request)
    session = await _resolve_session(request, store, session_id, tenant)
    await require_device_access(request, session, tenant)
    _refuse_local_folder(session)

    try:
        body.validate_spec()
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(),
        )

    try:
        async with _artifact_files(request, session) as files:
            meta = await _store(files, session).update(
                artifact_id, name=body.name, kind=body.kind, spec=body.spec,
            )
    except ArtifactNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Artifact {artifact_id} not found.",
        )
    except ArtifactLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(exc),
        )

    await store.emit_event(session_id, EventType.ARTIFACT_UPDATED, artifact_event(meta))

    return meta
