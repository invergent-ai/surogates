"""Office documents in the document server: editor configs, the file it
fetches, and the saves it calls back with."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from surogates.api.routes.sessions import SendMessageRequest, SendMessageResponse, deliver_message
from surogates.api.routes.workspace import (
    _get_session_store,
    _get_workspace_session_bucket_and_root,
    _require_service_account_api_route,
    _validate_path,
    workspace_files,
)
from surogates.api.session_guards import is_scheduled_run_session, require_device_access
from surogates.channels.inbound import build_principal_stamp
from surogates.db.models import User
from surogates.devices.workspace import MAX_READ_BYTES, DeviceWorkspaceIO
from surogates.office import sessions as office_sessions
from surogates.office.agent import AGENT_KINDS, location_of
from surogates.office.chats import live_session
from surogates.office.editor_config import build_editor_config
from surogates.office.files import DOCUMENT_FILES, FolderNotReady, document_files, document_session, on_disk_spelling
from surogates.office.guard import office_tools_offered
from surogates.office.kinds import office_kind
from surogates.office.mentions import MENTION_SOURCE, agent_address, mention_message, mentionable_agent
from surogates.office.tokens import InvalidOfficeToken, read_document_server_body, read_url_token
from surogates.runtime import AgentRuntimeContext, agent_runtime_context_dep, rate_limit_dep
from surogates.session.events import EventType
from surogates.storage.tenant import agent_session_bucket
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext
from surogates.tenant.memberships import belongs_to

logger = logging.getLogger(__name__)

router = APIRouter()

PENDING_PREFIX = "_office/pending/"


class EditorConfigResponse(BaseModel):
    document_server_url: str
    config: dict[str, Any]
    #: Who a comment can mention: the chat's agent where it answers in the thread, nobody elsewhere.
    mentionable_users: list[dict[str, str]] = []


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
        if isinstance(files, DeviceWorkspaceIO):
            path = await on_disk_spelling(files, path)
        stat = await files.stat(await files.resolve(path))
        if stat is None:
            raise HTTPException(status_code=404, detail=f"File not found: {path}")
        if isinstance(files, DeviceWorkspaceIO) and stat.size > MAX_READ_BYTES:
            raise HTTPException(
                status_code=413,
                detail="The document exceeds 50 MB and cannot be displayed here. Open it from its folder on your computer instead.",
            )
    location = location_of(target, path)
    document = await _open_document(request, location=location, path=path, session_id=session.id, user=(user_id, user_name))
    edit = kind.editable and not view and not is_scheduled_run_session(session)
    config = build_editor_config(
        settings=office, row_id=document.id, doc_key=document.doc_key, path=path, kind=kind,
        user_id=user_id, user_name=user_name, theme=theme, access="edit" if edit else "view",
    )
    # The agent answers a comment only where it edits live, in a document it edits, from an editor that edits, in a
    # chat it is sent the office tools in.
    mentionable = (
        [mentionable_agent(session.agent_id, await _agent_name(request, session.agent_id))]
        if office.agent_editing and edit and kind.file_type in AGENT_KINDS and office_tools_offered(session) else []
    )
    return EditorConfigResponse(document_server_url=office.public_url.rstrip("/"), config=config, mentionable_users=mentionable)


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
            user = await db.scalar(select(User).where(User.id == tenant.user_id, belongs_to(tenant.org_id)))
        return f"user:{tenant.user_id}", user.display_name if user is not None else "You"
    raise HTTPException(status_code=403, detail="Only a person can open the editor.")


async def _agent_name(request: Request, agent_id: str) -> str:
    """The agent's display name, as its editor session shows it; "Agent" where the runtime config does not say."""
    cache = getattr(request.app.state, "runtime_config_cache", None)
    if cache is None:
        return "Agent"
    try:
        payload = await cache.get(agent_id)
    except Exception:  # an unknown agent or a cache blip: the name is only shown
        logger.warning("Could not read agent %s's name for the editor", agent_id, exc_info=True)
        return "Agent"
    return str((payload or {}).get("display_name") or "Agent")


class EditorMentionRequest(BaseModel):
    path: str = Field(max_length=4096)
    #: The comment's id from the editor's notice (``actionLink.action.data``): a reply's is its thread's. Word's is hex,
    #: a spreadsheet's and a deck's a {GUID}, a PDF's hex: nothing else reaches the note the model reads.
    comment_id: str = Field(pattern=r"^[A-Za-z0-9{}_-]{1,64}$")
    #: The comment or reply that mentioned the agent, as written.
    message: str = Field(min_length=1, max_length=10_000)
    emails: list[str] = Field(max_length=50)


@router.post(
    "/api/sessions/{session_id}/workspace/editor/mention", response_model=SendMessageResponse, status_code=202,
)
@router.post("/sessions/{session_id}/workspace/editor/mention", response_model=SendMessageResponse, status_code=202)
async def mention_agent(
    session_id: UUID,
    body: EditorMentionRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    agent_runtime: AgentRuntimeContext = Depends(agent_runtime_context_dep),
    _rate: None = Depends(rate_limit_dep),
) -> SendMessageResponse:
    """A comment in the chat's file pane mentioned the agent: the chat gets it as the person's message, and the agent
    answers in the comment's thread."""
    _require_service_account_api_route(request, tenant)
    office = request.app.state.settings.office
    if not office.agent_editing:
        raise HTTPException(status_code=503, detail="The agent cannot answer comments here.")
    _validate_path(body.path)
    kind = office_kind(body.path)
    if kind is None or kind.file_type not in AGENT_KINDS:
        raise HTTPException(status_code=415, detail="The agent answers comments in Word documents, spreadsheets, slide decks and PDFs.")
    store = _get_session_store(request)
    session, _bucket, _root = await _get_workspace_session_bucket_and_root(request, store, session_id, tenant, path=body.path)
    await require_device_access(request, session, tenant)
    # Without office_read and office_edit the agent could not answer in the thread.
    if not office_tools_offered(session):
        raise HTTPException(status_code=409, detail="The agent in this chat cannot answer in comments.")
    # The address the editor offered: made of the agent's name as it is now.
    agent_name = await _agent_name(request, session.agent_id)
    if agent_address(agent_name).lower() not in {email.lower() for email in body.emails}:
        raise HTTPException(status_code=400, detail="The comment does not mention the agent.")
    author_id, author = await _acting_user(request, tenant)
    # A mention's message needs a name, and a person's display name may be empty.
    author = author or "Someone"
    target = await _document_session(request, session)
    # On a computer the pane may name the file in another spelling than its open row: compared without case there.
    documents = await office_sessions.open_like(request.app.state.session_factory, [location_of(target, body.path)])
    if not any(session.id in document.session_ids for document in documents):
        raise HTTPException(
            status_code=409, detail=f"{body.path} is not open in this chat any more: open it here again to mention the agent.",
        )
    content, metadata = mention_message(
        path=body.path, thread=body.comment_id, kind=kind.file_type, author=author, author_id=author_id,
        comment=body.message, agent_name=agent_name,
    )
    return await deliver_message(
        request, session_id, SendMessageRequest(content=content, metadata=metadata), tenant, agent_runtime,
        extra={"source": MENTION_SOURCE,
               **build_principal_stamp(user_id=tenant.user_id, service_account_id=tenant.service_account_id)},
    )


@router.get("/office/files/{token}")
async def get_office_file(token: str, request: Request) -> Response:
    """The document the document server opens: read through the workspace layer."""
    document = await _document_for(request, token, "file")
    # Its key's session has ended: a fresh open would start from bytes about to be replaced.
    if (document.pending_save or {}).get("close"):
        raise HTTPException(status_code=409, detail="The document is still being saved; try again in a moment.")
    async with _files_of(request, document) as files:
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
    # The pending area is the editor's, outside every chat's and case's files: a case's document keeps it in its
    # workspace's bucket, and a chat on a computer keeps its bucket stamp.
    if document.case_id is not None:
        bucket = agent_session_bucket(request.app.state.settings.storage.bucket)
    else:
        bucket = (await _live_session(request, document)).config["storage_bucket"]
    key = f"{PENDING_PREFIX}{document.doc_key}/{uuid4().hex}"
    storage = request.app.state.storage
    await storage.write(bucket, key, data)
    changes = _changes(payload)
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


@asynccontextmanager
async def _files_of(request: Request, document: office_sessions.OpenDocument) -> AsyncIterator[Any]:
    """The files the document is read from: its case's workspace, or its chat's, as the chat's pane reaches them."""
    files = document_files(document, storage=request.app.state.storage, settings=request.app.state.settings)
    if files is not None:
        yield files
        return
    target = await _document_session(request, await _live_session(request, document))
    async with workspace_files(request, target) as files:
        yield files


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
