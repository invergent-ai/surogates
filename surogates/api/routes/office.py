"""Office documents in the document server: editor configs, the file it
fetches, and the saves it calls back with."""

from __future__ import annotations

import logging
from typing import Any, Literal
from urllib.parse import unquote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select

from surogates.api.routes.workspace import (
    _get_session_store,
    _get_workspace_session_bucket_and_root,
    _require_service_account_api_route,
    _validate_path,
    workspace_files,
)
from surogates.api.session_guards import require_user_writable_session
from surogates.db.models import User
from surogates.devices.binding import device_of
from surogates.office import sessions as office_sessions
from surogates.office.editor_config import build_editor_config
from surogates.office.kinds import office_kind
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext
from surogates.tools.workspace_io import StorageWorkspaceIO

logger = logging.getLogger(__name__)

router = APIRouter()


class EditorConfigResponse(BaseModel):
    document_server_url: str
    config: dict[str, Any]


@router.get("/api/sessions/{session_id}/workspace/editor", response_model=EditorConfigResponse)
@router.get("/sessions/{session_id}/workspace/editor", response_model=EditorConfigResponse)
async def get_workspace_editor(
    session_id: UUID,
    request: Request,
    path: str = Query(..., description="Relative path within the workspace"),
    theme: Literal["light", "dark"] = Query("light"),
    view: bool = Query(False, description="Open read-only"),
    tenant: TenantContext = Depends(get_current_tenant),
) -> EditorConfigResponse:
    _require_service_account_api_route(request, tenant)
    office = request.app.state.settings.office
    if not office.enabled:
        raise HTTPException(status_code=503, detail="No document server is configured to open office documents.")
    _validate_path(path)
    kind = office_kind(path)
    if kind is None:
        raise HTTPException(status_code=415, detail="This file is not an office document.")
    store = _get_session_store(request)
    session, _bucket, _root = await _get_workspace_session_bucket_and_root(
        request, store, session_id, tenant, path=path, change=True,
    )
    if device_of(session.config) is not None:
        raise HTTPException(status_code=409, detail="Documents in a folder on a computer cannot be opened in the editor yet.")
    user_id, user_name = await _acting_user(request, tenant)
    async with workspace_files(request, session) as files:
        if not isinstance(files, StorageWorkspaceIO):
            raise HTTPException(status_code=409, detail="This workspace cannot be opened in the editor yet.")
        key = await files.resolve(path)
        if await files.stat(key) is None:
            raise HTTPException(status_code=404, detail=f"File not found: {path}")
        location = files.location(key)
    document = await office_sessions.open_or_reuse(
        request.app.state.session_factory, location=location, path=path, session_id=session.id,
    )
    config = build_editor_config(
        settings=office, row_id=document.id, doc_key=document.doc_key, path=path, kind=kind,
        user_id=user_id, user_name=user_name, edit=kind.editable and not view and _writable(session), theme=theme,
    )
    return EditorConfigResponse(document_server_url=office.public_url.rstrip("/"), config=config)


async def _acting_user(request: Request, tenant: TenantContext) -> tuple[str, str]:
    """Who the editor shows: the token's user, or the person a service account acts for."""
    if tenant.service_account_id is not None:
        acting_id = request.headers.get("X-Surogate-Acting-User-Id", "").strip()
        if not acting_id:
            raise HTTPException(
                status_code=400,
                detail="A service account opening the editor names the person in X-Surogate-Acting-User-Id.",
            )
        name = unquote(request.headers.get("X-Surogate-Acting-User-Name", "")).strip() or acting_id
        return f"sa:{tenant.service_account_id}:{acting_id}", name
    if tenant.user_id is not None:
        async with request.app.state.session_factory() as db:
            user = await db.scalar(select(User).where(User.id == tenant.user_id, User.org_id == tenant.org_id))
        return f"user:{tenant.user_id}", user.display_name if user is not None else "You"
    raise HTTPException(status_code=403, detail="Only a person can open the editor.")


def _writable(session: Any) -> bool:
    try:
        require_user_writable_session(session)
    except HTTPException:
        return False
    return True
