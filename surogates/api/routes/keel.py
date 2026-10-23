"""Keel's routes: through ``keel-link``, Keel starts an agent's step session,
answers it or asks it for a revision, cancels it, and reads its status.  It
also lists, reads and writes a case's files: the workspace its step sessions
share; it gives a person the editor config of one of them; and it invites
staff into an organisation.

Mounted only on the keel-link app (:mod:`surogates.api.keel_link`), never
on the public API.  Every route checks the link key with
:func:`require_link`, and loads sessions directly: no tenant, no JWT.

An error is ``HTTPException(status, {"code", "message"})``, which answers
``{"detail": {"code", "message"}}``.  Its message names no case data, and
neither does the log: a case's inputs are a client's.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import tempfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Literal, TypeVar
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError

from surogates.channels.constants import API_CHANNEL, session_queue_priority
from surogates.channels.memory_boundary import CASE_BOUNDARY_PREFIX
from surogates.config import INTERRUPT_CHANNEL_PREFIX, enqueue_session
from surogates.db.models import User
from surogates.keel.case_files import (
    CaseFileError,
    case_key,
    case_path,
    case_workspace,
    download_case_file,
    list_case_files,
    write_case_file,
)
from surogates.keel.files import file_value
from surogates.keel.steps import (
    ANSWER_MARKER,
    KICKOFF_MARKER,
    REVISION_MARKER,
    StepContract,
    is_case_step,
    render_kickoff,
    render_message,
    step_contract_of,
)
from surogates.office.editor_config import build_editor_config
from surogates.office.kinds import office_kind
from surogates.office.sessions import DocumentClosing, OpenDocument, open_at, open_like, open_waiting
from surogates.session.provisioning import create_agent_session
from surogates.session.store import SessionNotFoundError, StepFinishedError
from surogates.storage.backend import TooLarge
from surogates.tenant.auth.jwt import signing_secret
from surogates.tenant.auth.service_account import ServiceAccountStore
from surogates.tenant.invitations import InvitationError, invite_member
from surogates.tenant.memberships import belongs_to

logger = logging.getLogger(__name__)

router = APIRouter()

_Body = TypeVar("_Body", bound=BaseModel)

#: The prompt-injection screen's name for a step's inputs.
_SCREEN_SOURCE = "keel_case_step"
#: The ``synthetic`` marker of each kind of message Keel sends a step.
_MESSAGE_MARKERS = {"answer": ANSWER_MARKER, "revision": REVISION_MARKER}
#: Why a cancelled step's session paused: its pause event's and its interrupt's reason.
_CANCELLED = "case_step_cancelled"


class StepSessionRequest(BaseModel):
    """Keel's request to start one agent step.  Keys this runtime does not know are ignored."""

    model_config = ConfigDict(extra="ignore")

    org_id: UUID
    case_type: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)
    # Keel's id for the session: the runtime creates it with exactly this one.
    session_id: UUID
    contract: StepContract

    @model_validator(mode="after")
    def _one_case_type(self) -> StepSessionRequest:
        # The session's owning account is this case type's: it must be the contract's.
        if self.case_type != self.contract.case_type:
            raise ValueError("case_type differs from the contract's")
        return self


class StepSessionCreated(BaseModel):
    session_id: UUID
    service_account_id: str
    # Null: the worker resolves the agent's bundle when it wakes the session.
    bundle_version: str | None = None
    created: bool


class StepMessage(BaseModel):
    """A person's message to a step: an answer to its raised hand, or a draft's revision request."""

    model_config = ConfigDict(extra="ignore")

    kind: Literal["answer", "revision"]
    text: str = Field(min_length=1, max_length=20000)
    note: str | None = Field(default=None, max_length=20000)
    idempotency_key: str = Field(min_length=1, max_length=200)


class StepSessionStatus(BaseModel):
    status: str
    updated_at: datetime


class CaseEditorRequest(BaseModel):
    """Keel's request for one person's editor on a case's document.  Keys this runtime does not know are ignored."""

    model_config = ConfigDict(extra="ignore")

    org_id: UUID
    path: str
    user_id: UUID
    access: Literal["edit", "comment", "view"]
    theme: Literal["light", "dark"] = "light"


def _printable(value: str) -> str:
    if not value.isprintable():
        raise ValueError("holds a character that is not printable")
    return value


class MemberInvitationRequest(BaseModel):
    """Keel's invitation of one person into an organisation.  Keys this runtime does not know are ignored."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(min_length=1, max_length=200)

    @field_validator("email")
    @classmethod
    def _an_email(cls, value: str) -> str:
        local, _, domain = value.partition("@")
        if not local or not domain or "@" in domain or any(ch.isspace() for ch in value):
            raise ValueError("not an email")
        return _printable(value)

    @field_validator("display_name")
    @classmethod
    def _a_name(cls, value: str) -> str:
        return _printable(value)


def _refused(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code, {"code": code, "message": message})


def _database_failed(exc: DBAPIError, *, logged: str, message: str) -> HTTPException:
    """The generic 500 for a failed database call, logged by the error's class name alone.

    The error's text quotes the statement's parameters, the case's data among them.
    """
    logger.warning("keel-link: %s (%s)", logged, type(exc).__name__)
    return _refused(status.HTTP_500_INTERNAL_SERVER_ERROR, "internal-error", message)


def require_link(request: Request) -> None:
    """Refuse a call that does not show the link key; with no key set, refuse every call."""
    expected = request.app.state.settings.keel.link_key
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    if (
        not expected
        or scheme.lower() != "bearer"
        or not hmac.compare_digest(presented.strip().encode(), expected.encode())
    ):
        raise _refused(
            status.HTTP_401_UNAUTHORIZED, "bad-link-key", "The link key is missing or wrong.",
        )


async def read_body(request: Request, model: type[_Body]) -> _Body:
    """The request's JSON body as *model*; 422 ``invalid-request`` when it does not validate."""
    try:
        return model.model_validate(await request.json())
    except (ValueError, ValidationError):
        # Not the validation error's text: it would quote the case's data.
        raise _refused(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid-request", "The request body does not validate.",
        ) from None


async def _resolve_agent(request: Request, agent_id: str, org_id: UUID) -> dict[str, Any]:
    """The agent's runtime config, when it is the org's and running."""
    cache = request.app.state.runtime_config_cache
    unknown = _refused(status.HTTP_404_NOT_FOUND, "unknown-agent", "No such agent in this org.")
    for attempt in (1, 2):
        try:
            payload = await cache.get(agent_id)
            break
        except LookupError:
            raise unknown from None
        except Exception as exc:
            if attempt == 2:
                logger.warning(
                    "keel-link: agent %s could not be resolved (%s)", agent_id, type(exc).__name__,
                )
                raise _refused(
                    status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable", "The agent could not be resolved; try again.",
                ) from None
    try:
        same_org = UUID(str(payload.get("org_id"))) == org_id
    except ValueError:
        same_org = False
    # Another org's agent is not told apart from no agent at all.
    if not same_org:
        raise unknown
    if not payload.get("enabled"):
        raise _refused(status.HTTP_409_CONFLICT, "agent-stopped", "The agent is stopped.")
    return payload


def _screen_inputs(contract: StepContract) -> None:
    """Refuse inputs the prompt-injection screen blocks, before anything is written."""
    from surogates.api.routes.sessions import _get_injection_detector

    text = json.dumps(contract.inputs, indent=2, sort_keys=True, ensure_ascii=False)
    result = _get_injection_detector().detect(text, source=_SCREEN_SOURCE)
    if result.is_injection:
        logger.warning("keel-link: the inputs of case %s were blocked", contract.case_id)
        raise _refused(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "inputs-blocked", "The step's inputs were blocked.",
        )


def _key_in_use() -> HTTPException:
    return _refused(
        status.HTTP_409_CONFLICT, "key-in-use", "The key or the session id names another step's session.",
    )


async def _start(request: Request, session: Any, contract: StepContract, key: str) -> None:
    """Write the step's kickoff unless the session has it, and queue the session when this call wrote it.

    Calls racing on one key, as a retry of a call still running, write it once.
    """
    written = await request.app.state.session_store.emit_keyed_synthetic_message(
        session.id, content=render_kickoff(contract), synthetic=KICKOFF_MARKER, key=key,
    )
    if written is None:
        return
    await enqueue_session(
        request.app.state.redis,
        org_id=str(session.org_id),
        agent_id=session.agent_id,
        session_id=session.id,
        priority=session_queue_priority(session),
    )


async def _found(
    request: Request, existing: Any, body: StepSessionRequest, agent_id: str,
) -> JSONResponse:
    """The session a key already holds: this very step's, or the key is in use."""
    held = step_contract_of(existing)
    if (
        existing.id != body.session_id
        or existing.agent_id != agent_id
        or held is None
        or held.case_id != body.contract.case_id
        or held.token != body.contract.token
    ):
        raise _key_in_use()
    await _start(request, existing, body.contract, body.idempotency_key)
    return _answer(existing, created=False)


async def _exists(store: Any, session_id: UUID) -> bool:
    try:
        await store.get_session(session_id)
    except SessionNotFoundError:
        return False
    return True


def _answer(session: Any, *, created: bool) -> JSONResponse:
    answer = StepSessionCreated(
        session_id=session.id,
        service_account_id=str(session.service_account_id),
        created=created,
    )
    return JSONResponse(
        answer.model_dump(mode="json"),
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@router.post("/keel/step-sessions")
async def create_step_session(request: Request) -> JSONResponse:
    """Start the agent's session for one step of a case: 201 made, or 200 found by its key."""
    require_link(request)
    body = await read_body(request, StepSessionRequest)
    agent_id = request.query_params.get("agent_id") or ""
    if not agent_id:
        raise _refused(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid-request", "The agent_id query parameter is required.",
        )
    await _resolve_agent(request, agent_id, body.org_id)
    contract = body.contract
    _screen_inputs(contract)

    try:
        return await _create_or_find(request, body, agent_id)
    except DBAPIError as exc:
        raise _database_failed(
            exc,
            logged=f"the step session of case {contract.case_id} could not be written",
            message="The step session could not be created.",
        ) from None


async def _create_or_find(request: Request, body: StepSessionRequest, agent_id: str) -> JSONResponse:
    """Find the session the key holds, or create it; either way, give it its kickoff."""
    contract = body.contract
    store = request.app.state.session_store
    existing = await store.get_session_by_idempotency_key(body.org_id, body.idempotency_key)
    if existing is not None:
        return await _found(request, existing, body, agent_id)

    account_id = await ServiceAccountStore(request.app.state.session_factory).case_type_account(
        body.org_id, body.case_type,
    )
    boundary = f"{CASE_BOUNDARY_PREFIX}{contract.case_id}"
    config: dict[str, Any] = {"memory_boundary": boundary, "workspace_boundary": boundary}
    if contract.mcp_servers is not None:
        config["entitlements"] = {"mcp_server_ids": list(contract.mcp_servers)}
    try:
        session = await create_agent_session(
            store=store,
            storage=request.app.state.storage,
            settings=request.app.state.settings,
            org_id=body.org_id,
            user_id=None,
            agent_id=agent_id,
            channel=API_CHANNEL,
            config=config,
            service_account_id=account_id,
            idempotency_key=body.idempotency_key,
            session_id=body.session_id,
            case_step=contract.model_dump(mode="json"),
            case_scope={"tools": contract.tools, "knowledge": contract.knowledge},
        )
    except IntegrityError:
        # A concurrent call with the key made it first, or the id is taken.
        existing = await store.get_session_by_idempotency_key(body.org_id, body.idempotency_key)
        if existing is not None:
            return await _found(request, existing, body, agent_id)
        if await _exists(store, body.session_id):
            raise _key_in_use() from None
        # Another constraint broke: the route answers it as any database failure.
        raise

    await _start(request, session, contract, body.idempotency_key)
    return _answer(session, created=True)


def _unknown_session() -> HTTPException:
    return _refused(status.HTTP_404_NOT_FOUND, "unknown-session", "No such step session.")


async def _step_session(request: Request, session_id: str) -> Any:
    """The step session *session_id* names: 404 ``unknown-session`` for any other id."""
    try:
        session_uuid = UUID(session_id)
    except ValueError:
        raise _unknown_session() from None
    try:
        session = await request.app.state.session_store.get_session(session_uuid)
    except SessionNotFoundError:
        raise _unknown_session() from None
    # A child of the step works on the case, and a chat on none: neither is the step's.
    if not is_case_step(session):
        raise _unknown_session()
    return session


@router.post("/keel/step-sessions/{session_id}/messages")
async def send_step_message(session_id: str, request: Request) -> JSONResponse:
    """Give a step a person's answer or revision request, once per key, and wake it: 202.

    The message is written, and a session at rest resumed, only when this
    call is the first with the key; only then is the session queued.  A step
    that is over, cancelled among others, takes none: 409 ``step-finished``.
    """
    require_link(request)
    try:
        session = await _step_session(request, session_id)
        body = await read_body(request, StepMessage)
        written = await request.app.state.session_store.emit_keyed_synthetic_message(
            session.id,
            content=render_message(body.kind, body.text, body.note),
            synthetic=_MESSAGE_MARKERS[body.kind],
            key=body.idempotency_key,
            resume=True,
        )
    except SessionNotFoundError:
        # Deleted since the lookup above.
        raise _unknown_session() from None
    except StepFinishedError:
        raise _refused(
            status.HTTP_409_CONFLICT, "step-finished", "The step is over; it takes no more messages.",
        ) from None
    except DBAPIError as exc:
        raise _database_failed(
            exc,
            logged=f"the message to step session {session_id} could not be written",
            message="The message could not be delivered.",
        ) from None
    if written is not None:
        await enqueue_session(
            request.app.state.redis,
            org_id=str(session.org_id),
            agent_id=session.agent_id,
            session_id=session.id,
            priority=session_queue_priority(session),
        )
    return JSONResponse({"delivered": written is not None}, status_code=status.HTTP_202_ACCEPTED)


@router.post("/keel/step-sessions/{session_id}/cancel")
async def cancel_step_session(session_id: str, request: Request) -> JSONResponse:
    """End a step: redact its inputs, pause its session and interrupt its turn; 200, also when paused already.

    Every cancel interrupts the turn, so a retry stops a turn its first
    attempt did not reach; only the first pauses the session.
    """
    require_link(request)
    try:
        session = await _step_session(request, session_id)
        await request.app.state.session_store.cancel_case_step(session.id, {"reason": _CANCELLED})
    except SessionNotFoundError:
        # Deleted since the lookup above.
        raise _unknown_session() from None
    except DBAPIError as exc:
        raise _database_failed(
            exc,
            logged=f"step session {session_id} could not be cancelled",
            message="The step session could not be cancelled.",
        ) from None
    await request.app.state.redis.publish(
        f"{INTERRUPT_CHANNEL_PREFIX}:{session.id}", json.dumps({"reason": _CANCELLED}),
    )
    return JSONResponse({"status": "paused"})


@router.get("/keel/step-sessions/{session_id}")
async def read_step_session(session_id: str, request: Request) -> JSONResponse:
    """A step session's status, and when it last changed."""
    require_link(request)
    try:
        session = await _step_session(request, session_id)
    except DBAPIError as exc:
        raise _database_failed(
            exc,
            logged=f"step session {session_id} could not be read",
            message="The step session could not be read.",
        ) from None
    answer = StepSessionStatus(status=session.status, updated_at=session.updated_at)
    return JSONResponse(answer.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# A case's files
# ---------------------------------------------------------------------------


def _case_id(case_id: str) -> UUID:
    try:
        return UUID(case_id)
    except ValueError:
        raise _refused(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid-request", "The case id is not a UUID.",
        ) from None


def _checked_path(request: Request, case: UUID, path: str) -> str:
    """*path*, relative to the case's workspace: 422 ``invalid-path`` for one Keel may not name."""
    try:
        relative = case_path(path)
        case_key(request.app.state.settings, case, relative)
    except CaseFileError as exc:
        raise _refused(
            status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code, "The path is not one of the case's files.",
        ) from None
    return relative


def _case_path(request: Request, case: UUID) -> str:
    """The ``path`` query parameter, relative to the case's workspace: 422 ``invalid-path`` for one Keel may not name."""
    return _checked_path(request, case, request.query_params.get("path") or "")


def _storage_failed(exc: Exception, case: UUID) -> HTTPException:
    """The 503 for a store that would not answer (refused, throttled, unreachable), logged by the error's class name.

    The error's text can quote the object's key: a case's file name.
    """
    logger.warning("keel-link: the files of case %s could not be reached (%s)", case, type(exc).__name__)
    return _refused(
        status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable", "The case's files could not be reached; try again.",
    )


def _unknown_file() -> HTTPException:
    return _refused(status.HTTP_404_NOT_FOUND, "unknown-file", "No such file in the case's workspace.")


def _too_large(limit: int) -> HTTPException:
    return _refused(status.HTTP_413_CONTENT_TOO_LARGE, "too-large", f"The file is larger than {limit} bytes.")


def _editing(document: OpenDocument) -> list[str]:
    """Who is in the document's editor now, each once, by the name they opened it under, sorted."""
    return sorted(
        document.user_names.get(user) or ("An agent" if user.startswith("agent:") else "Someone")
        for user in dict.fromkeys(document.editors)
    )


@router.get("/keel/cases/{case_id}/files")
async def list_files_of_case(case_id: str, request: Request) -> JSONResponse:
    """The files of a case's workspace, by path, each with who is in its editor now."""
    require_link(request)
    case = _case_id(case_id)
    state = request.app.state
    try:
        files = await list_case_files(state.storage, state.settings, case)
    except (ClientError, BotoCoreError) as exc:
        raise _storage_failed(exc, case) from None
    workspace = case_workspace(state.storage, state.settings, case)
    try:
        documents = await open_like(state.session_factory, [workspace.location(file.path) for file in files])
    except DBAPIError as exc:
        raise _database_failed(
            exc,
            logged=f"the open documents of case {case} could not be read",
            message="The case's files could not be listed.",
        ) from None
    editing = {document.location: _editing(document) for document in documents}
    return JSONResponse({
        "files": [
            {
                "path": file.path,
                "name": PurePosixPath(file.path).name,
                "size": file.size,
                "modified_at": file.modified_at.isoformat(),
                "editing": editing.get(workspace.location(file.path), []),
            }
            for file in files
        ],
    })


#: How much of a file a read sends at once.
_CHUNK = 1 << 20


def _scratch_file() -> Path:
    handle, name = tempfile.mkstemp(prefix="keel-case-file-")
    os.close(handle)
    return Path(name)


def _opened_digest(path: Path) -> tuple[BinaryIO, str]:
    """*path* opened, and removed, so nothing is left behind however its reader ends; and its digest."""
    source = open(path, "rb")
    try:
        path.unlink()
        digest = hashlib.sha256()
        while chunk := source.read(_CHUNK):
            digest.update(chunk)
        source.seek(0)
    except BaseException:
        source.close()
        raise
    return source, digest.hexdigest()


async def _streamed(source: BinaryIO) -> AsyncIterator[bytes]:
    try:
        while chunk := await asyncio.to_thread(source.read, _CHUNK):
            yield chunk
    finally:
        source.close()


@router.get("/keel/cases/{case_id}/files/content")
async def read_file_of_case(case_id: str, request: Request) -> StreamingResponse:
    """A file of the case's workspace, streamed, with its digest in ``X-Content-SHA256``.

    The file is copied to a scratch file first, a piece at a time: its digest
    is known before its first byte is sent, and none of it is held whole.
    One over the size Keel may write is 413 ``too-large``, copied no further.
    """
    require_link(request)
    case = _case_id(case_id)
    relative = _case_path(request, case)
    state = request.app.state
    limit = state.settings.keel.max_file_bytes
    target = await asyncio.to_thread(_scratch_file)
    try:
        size = await download_case_file(state.storage, state.settings, case, relative, target, limit=limit)
        source, digest = await asyncio.to_thread(_opened_digest, target)
    except FileNotFoundError:
        raise _unknown_file() from None
    except TooLarge:
        raise _too_large(limit) from None
    except (ClientError, BotoCoreError) as exc:
        raise _storage_failed(exc, case) from None
    finally:
        target.unlink(missing_ok=True)
    return StreamingResponse(
        _streamed(source),
        media_type="application/octet-stream",
        headers={"X-Content-SHA256": digest, "Content-Length": str(size)},
    )


async def _body_within(request: Request, limit: int) -> bytes:
    """The request's body, read no further than one byte past *limit*: 413 ``too-large`` past it."""
    too_large = _too_large(limit)
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise too_large
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise too_large
    return bytes(body)


@router.put("/keel/cases/{case_id}/files/content")
async def write_file_of_case(case_id: str, request: Request) -> JSONResponse:
    """Write the body at the path in the case's workspace, over any file there: the file's Keel value and size.

    A file open in the editor is refused, 409 ``file-open``: the editor's next
    save would land over a file it never saw.
    """
    require_link(request)
    case = _case_id(case_id)
    relative = _case_path(request, case)
    state = request.app.state
    data = await _body_within(request, state.settings.keel.max_file_bytes)
    location = case_workspace(state.storage, state.settings, case).location(relative)
    try:
        open_now = await open_at(state.session_factory, location)
    except DBAPIError as exc:
        raise _database_failed(
            exc,
            logged=f"the open documents of case {case} could not be read",
            message="The file could not be written.",
        ) from None
    if open_now is not None:
        raise _refused(status.HTTP_409_CONFLICT, "file-open", "The file is open in the editor.")
    try:
        await write_case_file(state.storage, state.settings, case, relative, data)
    except (ClientError, BotoCoreError) as exc:
        raise _storage_failed(exc, case) from None
    value = await asyncio.to_thread(file_value, case, relative, data)
    return JSONResponse({**value, "size": len(data)})


async def _member_name(request: Request, org_id: UUID, user_id: UUID) -> str:
    """The display name of *user_id*, a user of *org_id*: 404 ``unknown-user`` for anyone else."""
    try:
        async with request.app.state.session_factory() as db:
            found = (
                await db.execute(select(User.display_name).where(User.id == user_id, belongs_to(org_id)))
            ).one_or_none()
    except DBAPIError as exc:
        raise _database_failed(
            exc, logged=f"user {user_id} could not be read", message="The editor could not be opened.",
        ) from None
    if found is None:
        raise _refused(status.HTTP_404_NOT_FOUND, "unknown-user", "No such user in this org.")
    return found.display_name


@router.post("/keel/cases/{case_id}/editor")
async def open_case_document(case_id: str, request: Request) -> JSONResponse:
    """One person's editor config for a document of the case's workspace, with the access Keel gives them.

    The person is the runtime user, under their display name, as when they
    open a file in an agent's chat.  Everyone who opens the file, and any
    step session of the case that has it open, shares its one editing
    session.  A kind the editor only converts to view opens read-only,
    whatever the access.
    """
    require_link(request)
    case = _case_id(case_id)
    body = await read_body(request, CaseEditorRequest)
    state = request.app.state
    office = state.settings.office
    if not office.enabled:
        raise _refused(status.HTTP_503_SERVICE_UNAVAILABLE, "office-off", "No document server is configured.")
    relative = _checked_path(request, case, body.path)
    kind = office_kind(relative)
    if kind is None:
        raise _refused(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "not-a-document", "The file is not an office document.",
        )
    workspace = case_workspace(state.storage, state.settings, case)
    try:
        found = await workspace.stat(relative)
    except (ClientError, BotoCoreError) as exc:
        raise _storage_failed(exc, case) from None
    if found is None:
        raise _unknown_file()
    editor_id = f"user:{body.user_id}"
    name = await _member_name(request, body.org_id, body.user_id)
    try:
        document = await open_waiting(
            state.session_factory, location=workspace.location(relative), path=relative,
            session_id=None, case_id=case, user=(editor_id, name),
        )
    except DocumentClosing:
        raise _refused(
            status.HTTP_409_CONFLICT, "document-closing", "The document is still being saved; try again in a moment.",
        ) from None
    except DBAPIError as exc:
        raise _database_failed(
            exc,
            logged=f"a document of case {case} could not be opened",
            message="The editor could not be opened.",
        ) from None
    config = build_editor_config(
        settings=office, row_id=document.id, doc_key=document.doc_key, path=relative, kind=kind,
        user_id=editor_id, user_name=name, access=body.access if kind.editable else "view", theme=body.theme,
    )
    return JSONResponse({"document_server_url": office.public_url.rstrip("/"), "config": config})


# ---------------------------------------------------------------------------
# Staff invitations
# ---------------------------------------------------------------------------

#: What each refused invitation answers.
_INVITATION_REFUSALS = {
    "unknown-org": (status.HTTP_404_NOT_FOUND, "No such org."),
    "ambiguous-email": (
        status.HTTP_409_CONFLICT, "More than one account of other orgs has the email: none is chosen.",
    ),
    "other-firebase-project": (
        status.HTTP_409_CONFLICT, "The account with the email signs in with a Firebase project this org does not use.",
    ),
}


def _org_id(org_id: str) -> UUID:
    try:
        return UUID(org_id)
    except ValueError:
        raise _refused(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid-request", "The org id is not a UUID.",
        ) from None


async def _firebase_project(request: Request, org_id: UUID) -> str | None:
    """The Firebase project the org signs in with, None when it has none: 503 ``unavailable`` when it cannot be read.

    A failure is no "no Firebase": a password account created in an org
    that signs in with Firebase would have no way in.
    """
    cache = getattr(request.app.state, "firebase_config_cache", None)
    if cache is None:
        return None
    try:
        config = await cache.get(str(org_id))
    except LookupError:
        return None
    except Exception as exc:  # noqa: BLE001 — the platform's failures, of any kind
        logger.warning(
            "keel-link: the Firebase config of org %s could not be read (%s)", org_id, type(exc).__name__,
        )
        raise _refused(
            status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable", "The org's sign-in could not be read; try again.",
        ) from None
    return config.firebase_project_id


@router.post("/keel/orgs/{org_id}/members")
async def invite_org_member(org_id: str, request: Request) -> JSONResponse:
    """Invite a person into the org by email: find them, join them from another org, or create their account.

    A new password account without a password, whose home is the org, gets
    a set-password token, answered once here, never logged or stored, with
    ``Cache-Control: no-store``.  Neither the email nor the token is logged.
    """
    require_link(request)
    org = _org_id(org_id)
    body = await read_body(request, MemberInvitationRequest)
    firebase_project = await _firebase_project(request, org)
    try:
        async with request.app.state.session_factory() as db:
            invitation = await invite_member(
                db,
                org_id=org,
                email=body.email,
                display_name=body.display_name,
                firebase_project=firebase_project,
                secret=signing_secret(),
                now=datetime.now(UTC),
            )
            await db.commit()
    except InvitationError as exc:
        status_code, message = _INVITATION_REFUSALS[exc.code]
        raise _refused(status_code, exc.code, message) from None
    except DBAPIError as exc:
        raise _database_failed(
            exc,
            logged=f"an invitation into org {org} could not be written",
            message="The invitation could not be written.",
        ) from None
    expires_at = invitation.expires_at
    return JSONResponse(
        {
            "user_id": str(invitation.user_id),
            "created": invitation.created,
            "joined": invitation.joined,
            "sign_in": invitation.sign_in,
            "set_password_token": invitation.set_password_token,
            "expires_at": expires_at.isoformat() if expires_at is not None else None,
        },
        headers={"Cache-Control": "no-store"},
    )
