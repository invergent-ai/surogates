"""Office documents in the document server: editor configs, the file it
fetches, and the saves it calls back with."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import select

from surogates.api.routes.workspace import (
    _get_session_store,
    _get_workspace_session_bucket_and_root,
    _require_service_account_api_route,
    _validate_path,
    workspace_files,
)
from surogates.api.session_guards import is_scheduled_run_session, require_device_access
from surogates.db.models import User
from surogates.devices.operations import DeviceOperations
from surogates.devices.workspace import MAX_READ_BYTES, DeviceWorkspaceIO
from surogates.office import sessions as office_sessions
from surogates.office.agent import location_of
from surogates.office.chats import live_session
from surogates.office.editor_config import build_editor_config
from surogates.office.files import DOCUMENT_FILES, FolderNotReady, document_session, editor_invocation
from surogates.office.kinds import office_kind
from surogates.office.tokens import InvalidOfficeToken, read_document_server_body, read_url_token
from surogates.session.events import EventType
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter()

PENDING_PREFIX = "_office/pending/"


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
    await require_device_access(request, session, tenant)
    user_id, user_name = await _acting_user(request, tenant)
    target = await _document_session(request, session)
    async with workspace_files(request, target) as files:
        if not isinstance(files, DOCUMENT_FILES):
            raise HTTPException(status_code=409, detail="This workspace cannot be opened in the editor yet.")
        stat = await files.stat(await files.resolve(path))
        if stat is None:
            raise HTTPException(status_code=404, detail=f"File not found: {path}")
        if isinstance(files, DeviceWorkspaceIO) and stat.size > MAX_READ_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"{path} is over 50 MiB, the most a document on a computer can be to open in the editor.",
            )
    location = location_of(target, path)
    document = await _open_document(request, location=location, path=path, session_id=session.id, user=(user_id, user_name))
    config = build_editor_config(
        settings=office, row_id=document.id, doc_key=document.doc_key, path=path, kind=kind,
        user_id=user_id, user_name=user_name, theme=theme,
        edit=kind.editable and not view and not is_scheduled_run_session(session),
    )
    return EditorConfigResponse(document_server_url=office.public_url.rstrip("/"), config=config)


async def _open_document(
    request: Request, *, location: str, path: str, session_id: UUID, user: tuple[str, str],
) -> office_sessions.OpenDocument:
    """The document's open row, once a closing one's last save has landed."""
    try:
        return await office_sessions.open_waiting(
            request.app.state.session_factory, location=location, path=path, session_id=session_id, user=user,
        )
    except office_sessions.DocumentClosing:
        raise HTTPException(status_code=409, detail="The document is still being saved; try again in a moment.") from None


async def _acting_user(request: Request, tenant: TenantContext) -> tuple[str, str]:
    """Who the editor shows: the token's user, or the person a service account acts for."""
    if tenant.service_account_id is not None:
        acting_id = request.headers.get("X-Surogate-Acting-User-Id", "").strip()
        if not acting_id:
            raise HTTPException(
                status_code=400,
                detail="A service account opening the editor names the person in X-Surogate-Acting-User-Id.",
            )
        if ":" in acting_id or len(acting_id) > 128:
            raise HTTPException(
                status_code=400, detail="X-Surogate-Acting-User-Id is at most 128 characters, without ':'.",
            )
        name = unquote(request.headers.get("X-Surogate-Acting-User-Name", "")).strip() or acting_id
        if len(name) > 200:
            raise HTTPException(status_code=400, detail="X-Surogate-Acting-User-Name is at most 200 characters.")
        return f"sa:{tenant.service_account_id}:{acting_id}", name
    if tenant.user_id is not None:
        async with request.app.state.session_factory() as db:
            user = await db.scalar(select(User).where(User.id == tenant.user_id, User.org_id == tenant.org_id))
        return f"user:{tenant.user_id}", user.display_name if user is not None else "You"
    raise HTTPException(status_code=403, detail="Only a person can open the editor.")


@router.get("/office/files/{token}")
async def get_office_file(token: str, request: Request) -> Response:
    """The document the document server opens: read through the workspace layer."""
    document = await _document_for(request, token, "file")
    # Its key's session has ended: a fresh open would start from bytes about to be replaced.
    if (document.pending_save or {}).get("close"):
        raise HTTPException(status_code=409, detail="The document is still being saved; try again in a moment.")
    target = await _document_session(request, await _live_session(request, document))
    async with workspace_files(request, target) as files:
        if not isinstance(files, DOCUMENT_FILES):
            raise HTTPException(status_code=409, detail="This workspace cannot be opened in the editor yet.")
        key = await files.resolve(document.path)
        for _ in range(3):  # a version that matches the bytes served
            version = await files.etag(key)
            try:
                data = await files.read(key)
            except FileNotFoundError:
                raise HTTPException(status_code=404, detail="The document is gone.") from None
            if await files.etag(key) == version:
                break
        else:
            raise HTTPException(status_code=409, detail="The document keeps changing; try again.")
    await office_sessions.record_base_version(request.app.state.session_factory, document.id, version)
    return Response(content=data, media_type="application/octet-stream")


@router.post("/office/callback/{token}")
async def office_callback(token: str, request: Request) -> JSONResponse:
    """The document server's status reports and saves."""
    office = request.app.state.settings.office
    document = await _document_for(request, token, "callback")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="The callback is not JSON.") from None
    try:
        payload = read_document_server_body(office.jwt_secret, body, request.headers.get("Authorization"))
    except InvalidOfficeToken:
        raise HTTPException(status_code=403, detail="The callback is not signed by the document server.") from None
    # The signature proves the document server sent it, not which document it is about.
    if payload.get("key") != document.doc_key:
        raise HTTPException(status_code=403, detail="The callback is for another document.")
    factory = request.app.state.session_factory
    status_code = int(payload.get("status", 0))
    if status_code == 1:
        await office_sessions.record_editors(factory, document.id, [str(user) for user in payload.get("users") or []])
    elif status_code in (2, 6):
        await _take_save(request, document, payload, close=status_code == 2)
    elif status_code == 4:
        await office_sessions.close_when_saved(factory, document.id)
    elif status_code in (3, 7):
        logger.warning("document server could not save %s (status %d, key %s)", document.path, status_code, document.doc_key)
        await _announce(request, document, EventType.OFFICE_SAVE_FAILED, {"path": document.path, "status": status_code})
        if status_code == 3:
            await office_sessions.close_when_saved(factory, document.id)
    return JSONResponse({"error": 0})


async def _take_save(request: Request, document: office_sessions.OpenDocument, payload: dict[str, Any], *, close: bool) -> None:
    """Keep the edited file at once (the document server retries a callback only
    three times, seconds apart); a worker job lands it."""
    office = request.app.state.settings.office
    received = datetime.now(timezone.utc)
    url = payload.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="The save names no file.")
    try:
        data = await _download(_internal(str(url), office.internal_url))
    except httpx.HTTPError:
        logger.warning("could not download the save of %s (key %s)", document.path, document.doc_key, exc_info=True)
        raise HTTPException(status_code=502, detail="Could not download the saved document.") from None
    session = await _live_session(request, document)
    # A chat on a computer keeps its bucket stamp: the pending area is the editor's, outside every chat's files.
    bucket = session.config["storage_bucket"]
    key = f"{PENDING_PREFIX}{document.doc_key}/{uuid4().hex}"
    storage = request.app.state.storage
    await storage.write(bucket, key, data)
    changes = _changes(payload)
    previous = dict(document.pending_save or {})
    discarded = await office_sessions.set_pending(
        request.app.state.session_factory, document.id, bucket=bucket, key=key, close=close,
        lastsave=_saved_at(payload, received),
        marks=[str(payload["userdata"])] if payload.get("userdata") else [],
        # Every change since the document opened is listed; this save's own start at the last one already
        # landed (kept: its author may have gone on editing).
        authors=_authors(changes[max(document.changes_seen - 1, 0):]),
        changes=len(changes),
    )
    if discarded:
        try:
            await storage.delete(bucket, discarded)
        except Exception:  # the save is recorded; the stray object only costs storage
            logger.warning("could not delete the discarded save %s/%s", bucket, discarded, exc_info=True)
    if discarded and discarded == previous.get("key") and previous.get("waiting") == "waiting":
        # Its write may still be open on the computer: landing later, it would make this newer save a conflict copy.
        await _cancel_editor_write(request, session, document.doc_key, previous)


async def _cancel_editor_write(request: Request, session: Any, doc_key: str, pending: dict[str, Any]) -> None:
    try:
        target = await document_session(session, session_factory=request.app.state.session_factory)
        await DeviceOperations(request.app.state.session_factory, request.app.state.redis).cancel_invocation(
            target.id, editor_invocation(doc_key, pending),
        )
    except Exception:  # the newer save is kept either way; a write that still lands shows as a conflict copy
        logger.warning("could not cancel the replaced save of %s on its computer", doc_key, exc_info=True)


def _saved_at(payload: dict[str, Any], received: datetime) -> datetime:
    """When the document server made the save (its ``lastsave``), or *received* when it does not say."""
    try:
        when = datetime.fromisoformat(str(payload["lastsave"]))
    except (KeyError, ValueError):
        return received
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _changes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The document server's ``history.changes``: every change since the document opened, first first."""
    history = payload.get("history")
    changes = history.get("changes") if isinstance(history, dict) else None
    return [change for change in changes if isinstance(change, dict)] if isinstance(changes, list) else []


def _authors(changes: list[dict[str, Any]]) -> list[str]:
    """The editor user ids that made *changes*, each once, first first."""
    authors: list[str] = []
    for change in changes:
        user = change.get("user")
        user_id = str(user.get("id") or "") if isinstance(user, dict) else ""
        if user_id and user_id not in authors:
            authors.append(user_id)
    return authors


async def _download(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


def _internal(url: str, internal_url: str) -> str:
    """The document server's URL rewritten to how the runtime reaches it."""
    target, source = urlsplit(internal_url), urlsplit(url)
    return urlunsplit((target.scheme, target.netloc, source.path, source.query, source.fragment))


async def _document_for(request: Request, token: str, purpose: Literal["file", "callback"]) -> office_sessions.OpenDocument:
    office = request.app.state.settings.office
    if not office.enabled:
        raise HTTPException(status_code=503, detail="No document server is configured.")
    try:
        row_id = read_url_token(office.token_secret, token, purpose)
    except InvalidOfficeToken:
        raise HTTPException(status_code=403, detail="This link is not valid.") from None
    document = await office_sessions.get_open(request.app.state.session_factory, row_id)
    if document is None:
        raise HTTPException(status_code=404, detail="The document is no longer open.")
    return document


async def _live_session(request: Request, document: office_sessions.OpenDocument):
    session = await live_session(_get_session_store(request), document.session_ids)
    if session is None:
        raise HTTPException(status_code=404, detail="The document's chat is gone.")
    return session


async def _document_session(request: Request, session: Any) -> Any:
    """The session whose files hold *session*'s documents: a project thread on a computer reaches the real folder
    through its master."""
    try:
        return await document_session(session, session_factory=request.app.state.session_factory)
    except FolderNotReady as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


async def _announce(request: Request, document: office_sessions.OpenDocument, event_type: EventType, data: dict[str, Any]) -> None:
    store = _get_session_store(request)
    for session_id in document.session_ids:
        try:
            await store.emit_event(session_id, event_type, data)
        except Exception:
            logger.warning("could not tell session %s about %s", session_id, event_type.value, exc_info=True)
