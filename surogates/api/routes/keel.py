"""Keel's routes: through ``keel-link``, Keel starts an agent's step session,
answers it, asks it for a revision or sends it a message, gives it a comment
that mentions its agent, cancels it, and reads its status, its events, by
page and live, and its progress.  It also
lists, reads and writes a case's files: the workspace its step sessions
share; it gives a person the editor config of one of them; and it invites
staff into an organisation.

Mounted only on the keel-link app (:mod:`surogates.api.keel_link`), never
on the public API.  Every route checks the link key with
:func:`require_link`, and loads sessions directly: no tenant, no JWT.  The
public helpers here serve the other keel-link routers too: refusals,
database failures, the agent's resolution, the injection screen, the
create, lookup, queueing and cancel of a session Keel starts, and the reads
of its events, by page and live, and of its progress.

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
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, BinaryIO, Literal, Protocol, TypeVar
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sse_starlette.sse import EventSourceResponse

from surogates.api.routes.events import session_event_stream
from surogates.api.routes.office import agent_display_name
from surogates.channels.constants import API_CHANNEL, session_queue_priority
from surogates.channels.memory_boundary import CASE_BOUNDARY_PREFIX, rerun_kind_of
from surogates.config import INTERRUPT_CHANNEL_PREFIX, enqueue_session
from surogates.db.models import User
from surogates.harness.tool_schemas import FILE_PANE_KEY
from surogates.keel.case_files import (
    CaseFileError,
    case_key,
    case_path,
    case_workspace,
    download_case_file,
    list_case_files,
    record_case_org,
    write_case_file,
)
from surogates.keel.files import file_value
from surogates.keel.versions import MAX_ASKED, SHA256_HEX, VersionNotKept, case_versions
from surogates.keel.reruns import read_case_record
from surogates.keel.run_facts import bundle_version_in
from surogates.keel.steps import (
    ANSWER_MARKER,
    KICKOFF_MARKER,
    MENTION_MARKER,
    MESSAGE_MARKER,
    REVISION_MARKER,
    StepContract,
    is_case_step,
    is_rerun,
    render_kickoff,
    render_message,
    rests_on_its_report,
    step_contract_of,
)
from surogates.office.agent import AGENT_KINDS
from surogates.office.editor_config import build_editor_config
from surogates.office.guard import office_tools_offered
from surogates.office.kinds import office_kind
from surogates.office.mentions import MENTION_SOURCE, mention_message, mentionable_agent
from surogates.office.sessions import DocumentClosing, OpenDocument, open_at, open_under, open_waiting
from surogates.runtime.firebase_cache import FirebaseConfigUnavailable, project_firebase
from surogates.session.events import EventType
from surogates.session.models import Event
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

#: The prompt-injection screen's name for the case data Keel sends.
_SCREEN_SOURCE = "keel_case_step"
#: The ``synthetic`` marker of each kind of message Keel sends a step.
_MESSAGE_MARKERS = {"answer": ANSWER_MARKER, "revision": REVISION_MARKER, "message": MESSAGE_MARKER}
#: Why a cancelled step's session paused: its pause event's and its interrupt's reason.
_CANCELLED = "case_step_cancelled"
#: The largest event id Postgres holds: a cursor past it is refused.
_BIGINT_MAX = 2**63 - 1
#: How many events a page holds, unless Keel asks for fewer; and at most.
_PAGE_DEFAULT = 200
_PAGE_MAX = 500


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
    # The agent's bundle version at this call; null when the agent has none.
    bundle_version: str | None
    created: bool


def _printable(value: str) -> str:
    if not value.isprintable():
        raise ValueError("holds a character that is not printable")
    return value


def _a_name(value: str) -> str:
    if not value.strip():
        raise ValueError("is blank")
    return _printable(value)


#: The display name of the person or agent who sends Keel's plain message: 1 to 200 printable characters, not blank.
SenderName = Annotated[str, Field(min_length=1, max_length=200), AfterValidator(_a_name)]


class StepMessage(BaseModel):
    """Keel's message to a step: a person's answer to its raised hand, a draft's
    revision request, or a plain message.  A key this route does not know is refused."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["answer", "revision", "message"]
    text: str = Field(min_length=1, max_length=20000)
    # An answer's or a revision's alone.
    note: str | None = Field(default=None, max_length=20000)
    # A plain message's alone.
    sender: SenderName | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _fields_of_its_kind(self) -> StepMessage:
        if self.kind == "message" and self.note is not None:
            raise ValueError("a message takes no note")
        if self.kind != "message" and self.sender is not None:
            raise ValueError("only a message has a sender")
        return self


class MentionRequest(BaseModel):
    """A comment in one of the case's documents that mentions a draft step's
    agent.  A key this route does not know is refused."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)
    # The comment's thread as the editor names it: Word's hex, a spreadsheet's
    # or a deck's {GUID}, a PDF's hex.  Nothing else reaches the note the model reads.
    thread: str = Field(pattern=r"^[A-Za-z0-9{}_-]{1,64}$")
    comment: str = Field(min_length=1, max_length=20000)
    author_id: UUID
    # Null when Keel has no name for the person: the message says "Someone".
    author_name: SenderName | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)


class StepSessionStatus(BaseModel):
    status: str
    updated_at: datetime


#: A time answered in ISO 8601 with its offset, ``+00:00`` for UTC.
_IsoTime = Annotated[datetime, PlainSerializer(datetime.isoformat, when_used="json")]


class TodoItem(BaseModel):
    """One item of a session's todo list, as its agent's todo tool writes it.  Keys beyond these are left out."""

    # A stored item out of this shape is left out, never turned into one.
    model_config = ConfigDict(strict=True)

    id: str
    content: str
    status: str


class LastActivity(BaseModel):
    """A session's newest event that is not an ``llm.delta``; ``tool`` names a tool call's or result's tool."""

    event_id: int
    type: str
    at: _IsoTime
    tool: str | None


class StepProgress(BaseModel):
    status: str
    # None: the agent never wrote a list; []: it emptied it.
    todos: list[TodoItem] | None
    todos_at: _IsoTime | None
    last_activity: LastActivity | None


class MentionableAgent(BaseModel):
    """An agent Keel offers to mention in a case's document: the agent id, and Keel's name for it."""

    id: str = Field(min_length=1, max_length=200)
    name: Annotated[str, Field(min_length=1, max_length=200), AfterValidator(_printable)]


class CaseEditorRequest(BaseModel):
    """Keel's request for one person's editor on a case's document.  Keys this runtime does not know are ignored."""

    model_config = ConfigDict(extra="ignore")

    org_id: UUID
    path: str
    user_id: UUID
    access: Literal["edit", "comment", "view"]
    theme: Literal["light", "dark"] = "light"
    # The agents the editor offers to mention: a draft step's, whose agent answers in the comment's thread.
    mentionable: list[MentionableAgent] = Field(default=[], max_length=20)


def _sha256_hex(value: str) -> str:
    if not SHA256_HEX.fullmatch(value):
        raise ValueError("not a SHA-256 digest")
    return value


class MissingVersions(BaseModel):
    """Keel's question about which versions of a case's files the runtime no longer holds.  Keys this runtime does not know are ignored."""

    model_config = ConfigDict(extra="ignore")

    sha256: Annotated[list[Annotated[str, AfterValidator(_sha256_hex)]], Field(min_length=1, max_length=MAX_ASKED)]


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


def refused(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code, {"code": code, "message": message})


def database_failed(exc: DBAPIError, *, logged: str, message: str) -> HTTPException:
    """The generic 500 for a failed database call, logged by the error's class name alone.

    The error's text quotes the statement's parameters, the case's data among them.
    """
    logger.warning("keel-link: %s (%s)", logged, type(exc).__name__)
    return refused(status.HTTP_500_INTERNAL_SERVER_ERROR, "internal-error", message)


def database_unavailable(exc: DBAPIError, *, logged: str, message: str) -> HTTPException:
    """The 503 for a failed database read, which Keel tries again, logged by the error's class name alone.

    The error's text can quote the rows read, the case's data among them.
    """
    logger.warning("keel-link: %s (%s)", logged, type(exc).__name__)
    return refused(status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable", message)


def query_int(request: Request, name: str, *, default: int, low: int, high: int | None) -> int:
    """The query parameter *name* as an integer from *low* to *high*, or *default* when absent.

    Anything else is 422 ``invalid-request``.  Only ASCII digits make an
    integer here: no sign, no space.
    """
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        value = int(raw) if raw.isascii() and raw.isdecimal() else None
    except ValueError:
        # More digits than Python converts.
        value = None
    if value is None or value < low or (high is not None and value > high):
        bounds = f"from {low}" if high is None else f"from {low} to {high}"
        raise refused(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid-request",
            f"The {name} query parameter is not an integer {bounds}.",
        )
    return value


def _as_utc(value: datetime) -> datetime:
    """*value* in UTC, a value without an offset read as one in UTC: the database's clock is."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def require_link(request: Request) -> None:
    """Refuse a call that does not show the link key; with no key set, refuse every call."""
    expected = request.app.state.settings.keel.link_key
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    if (
        not expected
        or scheme.lower() != "bearer"
        or not hmac.compare_digest(presented.strip().encode(), expected.encode())
    ):
        raise refused(
            status.HTTP_401_UNAUTHORIZED, "bad-link-key", "The link key is missing or wrong.",
        )


async def read_body(request: Request, model: type[_Body]) -> _Body:
    """The request's JSON body as *model*; 422 ``invalid-request`` when it does not validate."""
    try:
        return model.model_validate(await request.json())
    except (ValueError, ValidationError):
        # Not the validation error's text: it would quote the case's data.
        raise refused(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid-request", "The request body does not validate.",
        ) from None


def agent_id_of(request: Request) -> str:
    """The ``agent_id`` query parameter of a create or an analysis call: 422 ``invalid-request`` when it is missing."""
    agent_id = request.query_params.get("agent_id") or ""
    if not agent_id:
        raise refused(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid-request", "The agent_id query parameter is required.",
        )
    return agent_id


async def resolve_agent(
    request: Request, agent_id: str, org_id: UUID, *, running: bool = True,
) -> dict[str, Any]:
    """The agent's runtime config, when it is the org's and, unless *running* is false, running.

    A call that only analyses what the agent already did (labelling its
    raised hands, say) passes ``running=False`` and is served for a
    stopped agent too.
    """
    cache = request.app.state.runtime_config_cache
    unknown = refused(status.HTTP_404_NOT_FOUND, "unknown-agent", "No such agent in this org.")
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
                raise refused(
                    status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable", "The agent could not be resolved; try again.",
                ) from None
    try:
        same_org = UUID(str(payload.get("org_id"))) == org_id
    except ValueError:
        same_org = False
    # Another org's agent is not told apart from no agent at all.
    if not same_org:
        raise unknown
    if running and not payload.get("enabled"):
        raise refused(status.HTTP_409_CONFLICT, "agent-stopped", "The agent is stopped.")
    return payload


def screen(value: object, *, case_id: UUID) -> None:
    """Refuse case data the prompt-injection screen blocks, before anything is written: 422 ``inputs-blocked``."""
    from surogates.api.routes.sessions import _get_injection_detector

    text = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
    result = _get_injection_detector().detect(text, source=_SCREEN_SOURCE)
    if result.is_injection:
        logger.warning("keel-link: the inputs of case %s were blocked", case_id)
        raise refused(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "inputs-blocked", "The case's inputs were blocked.",
        )


async def refuse_a_reruns_folder(request: Request, case_id: UUID) -> None:
    """409 ``case-exists`` for a case id whose boundary folder is a rerun's or a trial's.

    A case's session made under a rerun's id would share the rerun's folder and memory, and the rerun's purge
    would then delete the case's files.  A case's folder, even one with no record yet, is never marked.
    """
    state = request.app.state
    try:
        record = await read_case_record(state.storage, state.settings, case_id)
    except (ClientError, BotoCoreError) as exc:
        raise _storage_failed(exc, case_id) from None
    if record is not None and record.rerun is not None:
        raise refused(status.HTTP_409_CONFLICT, "case-exists", "The case id names a rerun or a trial.")


def _key_in_use() -> HTTPException:
    return refused(
        status.HTTP_409_CONFLICT, "key-in-use", "The key or the session id names another session.",
    )


async def queue_session(request: Request, session: Any) -> None:
    """Queue *session* for a worker to wake, at its priority: a case's work waits behind people."""
    await enqueue_session(
        request.app.state.redis,
        org_id=str(session.org_id),
        agent_id=session.agent_id,
        session_id=session.id,
        priority=session_queue_priority(session),
    )


async def kick_off(request: Request, session: Any, *, content: str, synthetic: str, key: str) -> None:
    """Write a session's first message, marked *synthetic*, unless it has it; queue it when this call wrote it.

    Calls racing on one key, as a retry of a call still running, write it once.
    """
    written = await request.app.state.session_store.emit_keyed_synthetic_message(
        session.id, content=content, synthetic=synthetic, key=key,
    )
    if written is not None:
        await queue_session(request, session)


class _KeelCreate(Protocol):
    """What :func:`create_or_find` reads of a create's body."""

    org_id: UUID
    idempotency_key: str
    session_id: UUID


class _CaseWorkCreate(_KeelCreate, Protocol):
    """What :func:`create_case_session` reads of a create's body: also its case type and its work contract."""

    case_type: str
    contract: Any


async def create_or_find(
    request: Request,
    body: _KeelCreate,
    *,
    matches: Callable[[Any], bool],
    make: Callable[[], Awaitable[Any]],
    start: Callable[[Any], Awaitable[None]],
) -> tuple[Any, bool]:
    """The session *body*'s key holds, or a new one made by *make*; either way started by *start*.

    A session the key holds is this one when it has Keel's id and *matches*
    says so; otherwise the key is in use, 409 ``key-in-use``, as is Keel's
    id once another session has it.  A found session is started again, so a
    retry writes a first message its first call lost.  Calls racing on one
    key make one session.  Answers the session, and whether this call made it.
    """
    store = request.app.state.session_store

    async def found(existing: Any) -> tuple[Any, bool]:
        if existing.id != body.session_id or not matches(existing):
            raise _key_in_use()
        await start(existing)
        return existing, False

    existing = await store.get_session_by_idempotency_key(body.org_id, body.idempotency_key)
    if existing is not None:
        return await found(existing)
    try:
        session = await make()
    except IntegrityError:
        # A concurrent call with the key made it first, or the id is taken.
        existing = await store.get_session_by_idempotency_key(body.org_id, body.idempotency_key)
        if existing is not None:
            return await found(existing)
        if await session_exists(store, body.session_id):
            raise _key_in_use() from None
        # Another constraint broke: the route answers it as any database failure.
        raise
    await start(session)
    return session, True


async def session_exists(store: Any, session_id: UUID) -> bool:
    try:
        await store.get_session(session_id)
    except SessionNotFoundError:
        return False
    return True


async def note_case_org(request: Request, case_id: UUID, org_id: UUID) -> None:
    """Record *org_id* as the case's org, unless one is recorded already, once a session of the case exists.

    A create never refuses on it: a failure is logged by the case id and the
    error's class name, and changes nothing.
    """
    state = request.app.state
    try:
        await record_case_org(state.storage, state.settings, case_id, org_id)
    except Exception as exc:
        # The error's text can name the record's key, or the org.
        logger.warning("keel-link: the org of case %s could not be recorded (%s)", case_id, type(exc).__name__)


def created_answer(session: Any, *, created: bool, bundle_version: str | None) -> JSONResponse:
    answer = StepSessionCreated(
        session_id=session.id,
        service_account_id=str(session.service_account_id),
        bundle_version=bundle_version,
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
    agent_id = agent_id_of(request)
    agent = await resolve_agent(request, agent_id, body.org_id)
    contract = body.contract
    screen(contract.inputs, case_id=contract.case_id)
    await refuse_a_reruns_folder(request, contract.case_id)

    def this_step(existing: Any) -> bool:
        held = step_contract_of(existing)
        return (
            existing.agent_id == agent_id
            and rerun_kind_of(existing) is None
            and held is not None
            and held.case_id == contract.case_id
            and held.token == contract.token
        )

    async def start(session: Any) -> None:
        await kick_off(
            request, session, content=render_kickoff(contract), synthetic=KICKOFF_MARKER, key=body.idempotency_key,
        )

    try:
        session, created = await create_or_find(
            request,
            body,
            matches=this_step,
            make=lambda: create_case_session(
                request, body, agent_id, file_pane=contract.mode == "draft", case_step=contract.model_dump(mode="json"),
            ),
            start=start,
        )
    except DBAPIError as exc:
        raise database_failed(
            exc,
            logged=f"the step session of case {contract.case_id} could not be written",
            message="The step session could not be created.",
        ) from None
    await note_case_org(request, contract.case_id, body.org_id)
    return created_answer(session, created=created, bundle_version=bundle_version_in(agent.get("bundle_version")))


async def create_case_session(
    request: Request, body: _CaseWorkCreate, agent_id: str, *, file_pane: bool = False, **work: dict[str, Any],
) -> Any:
    """Create the session Keel starts on a step or a work item of a case, with Keel's id and key.

    It is on ``api``, owned by the org's account for the body's case type,
    inside the case's ``case:`` memory and workspace boundaries, narrowed to
    the contract's MCP servers when it names them, and scoped to its tools
    and knowledge.  With *file_pane*, a draft step's, it shows the case's
    documents beside the person, so its agent may edit them.  *work* is the
    contract as it is stamped: ``case_step`` or ``case_item``.
    """
    contract = body.contract
    account_id = await ServiceAccountStore(request.app.state.session_factory).case_type_account(
        body.org_id, body.case_type,
    )
    boundary = f"{CASE_BOUNDARY_PREFIX}{contract.case_id}"
    config: dict[str, Any] = {"memory_boundary": boundary, "workspace_boundary": boundary}
    if file_pane:
        config[FILE_PANE_KEY] = True
    if contract.mcp_servers is not None:
        config["entitlements"] = {"mcp_server_ids": list(contract.mcp_servers)}
    return await create_agent_session(
        store=request.app.state.session_store,
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
        case_scope={"tools": contract.tools, "knowledge": contract.knowledge},
        **work,
    )


def unknown_session() -> HTTPException:
    """404 ``unknown-session``: no such session, or none the route serves."""
    return refused(status.HTTP_404_NOT_FOUND, "unknown-session", "No such session.")


async def session_where(request: Request, session_id: str, holds: Callable[[Any], bool]) -> Any:
    """The session *session_id* names, when *holds* says it is one this route serves.

    404 ``unknown-session`` for any other id, and for one that is not a UUID.
    """
    try:
        session_uuid = UUID(session_id)
    except ValueError:
        raise unknown_session() from None
    try:
        session = await request.app.state.session_store.get_session(session_uuid)
    except SessionNotFoundError:
        raise unknown_session() from None
    if not holds(session):
        raise unknown_session()
    return session


async def watched_session(request: Request, session_id: str, holds: Callable[[Any], bool]) -> Any:
    """The session a read of its events or its progress names, as :func:`session_where` finds it.

    A database failure is 503 ``unavailable``, which Keel tries again.
    """
    try:
        return await session_where(request, session_id, holds)
    except DBAPIError as exc:
        raise database_unavailable(
            exc,
            logged=f"session {session_id} could not be read",
            message="The session could not be read; try again.",
        ) from None


def _is_a_step(session: Any) -> bool:
    """Whether *session* is a step Keel started for a case, and no rerun's: a rerun is another route's."""
    return is_case_step(session) and not is_rerun(session)


async def _step_session(request: Request, session_id: str) -> Any:
    """The step session *session_id* names: 404 ``unknown-session`` for any other id.

    A child of the step works on the case, and a chat on none: neither is the step's.
    A rerun's session is a step's too, but never this route's.
    """
    return await session_where(request, session_id, _is_a_step)


@router.post("/keel/step-sessions/{session_id}/messages")
async def send_step_message(session_id: str, request: Request) -> JSONResponse:
    """Give a step Keel's message, once per key, and wake it: 202.

    The message is written, and a session at rest resumed, only when this
    call is the first with the key; only then is the session queued.  A step
    that is over, cancelled among others, takes none: 409 ``step-finished``.

    An answer or a revision gives the step back: it clears a report's mark.
    A plain message does not.  A step resting on its report takes it
    unwoken, for the turn Keel's next answer or revision wakes, and the
    answer says so in ``resting``, a repeat's included.
    """
    require_link(request)
    try:
        session = await _step_session(request, session_id)
        body = await read_body(request, StepMessage)
        written = await request.app.state.session_store.emit_keyed_synthetic_message(
            session.id,
            content=render_message(body.kind, body.text, body.note, body.sender),
            synthetic=_MESSAGE_MARKERS[body.kind],
            key=body.idempotency_key,
            resume=True,
            keep_report=body.kind == "message",
        )
    except SessionNotFoundError:
        # Deleted since the lookup above.
        raise unknown_session() from None
    except StepFinishedError:
        raise refused(
            status.HTTP_409_CONFLICT, "step-finished", "The step is over; it takes no more messages.",
        ) from None
    except DBAPIError as exc:
        raise database_failed(
            exc,
            logged=f"the message to step session {session_id} could not be written",
            message="The message could not be delivered.",
        ) from None
    if written is not None and not written.resting:
        await queue_session(request, session)
    answer: dict[str, bool] = {"delivered": written is not None}
    if body.kind == "message":
        # A repeat writes nothing: the row read above says whether the step rests.
        answer["resting"] = written.resting if written is not None else rests_on_its_report(session.config)
    return JSONResponse(answer, status_code=status.HTTP_202_ACCEPTED)


@router.post("/keel/step-sessions/{session_id}/mentions")
async def mention_step_agent(session_id: str, request: Request) -> JSONResponse:
    """A comment in one of the case's documents mentions a draft step's agent:
    give the step the message a chat's mention becomes, once per key, and wake it: ``202 {}``.

    Only a step whose agent edits the case's documents beside the person
    takes one: a draft step's, where the runtime has agent editing; any
    other is 409 ``no-office``.  The message wakes a draft under review
    too, and keeps its report's mark: the agent answers in the comment's
    thread, and the draft stays the reviewer's.  The session is queued only
    when this call wrote the message.  A step that is over takes none: 409
    ``step-finished``.  The route logs neither the comment, its author nor the path.
    """
    require_link(request)
    try:
        session = await _step_session(request, session_id)
        body = await read_body(request, MentionRequest)
        path = checked_path(request, step_contract_of(session).case_id, body.path)
        kind = office_kind(path)
        if kind is None or kind.file_type not in AGENT_KINDS:
            raise refused(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "not-a-document", "The file is not a document an agent edits.",
            )
        if not (request.app.state.settings.office.agent_editing and office_tools_offered(session)):
            raise refused(status.HTTP_409_CONFLICT, "no-office", "The step's session has no office tools.")
        content, metadata = mention_message(
            path=path,
            thread=body.thread,
            kind=kind.file_type,
            author=body.author_name or "Someone",
            author_id=f"user:{body.author_id}",
            comment=body.comment,
            agent_name=await agent_display_name(request, session.agent_id),
        )
        written = await request.app.state.session_store.emit_keyed_synthetic_message(
            session.id,
            content=content,
            synthetic=MENTION_MARKER,
            key=body.idempotency_key,
            extra={"source": MENTION_SOURCE, "metadata": metadata},
            resume=True,
            keep_report=True,
            wake_resting=True,
        )
    except SessionNotFoundError:
        # Deleted since the lookup above.
        raise unknown_session() from None
    except StepFinishedError:
        raise refused(
            status.HTTP_409_CONFLICT, "step-finished", "The step is over; it takes no more messages.",
        ) from None
    except DBAPIError as exc:
        raise database_failed(
            exc,
            logged=f"the mention to step session {session_id} could not be written",
            message="The mention could not be delivered.",
        ) from None
    if written is not None:
        await queue_session(request, session)
    return JSONResponse({}, status_code=status.HTTP_202_ACCEPTED)


@router.post("/keel/step-sessions/{session_id}/cancel")
async def cancel_step_session(session_id: str, request: Request) -> JSONResponse:
    """End a step: redact its inputs, pause its session and interrupt its turn; 200, also when paused already.

    Every cancel interrupts the turn, so a retry stops a turn its first
    attempt did not reach; only the first pauses the session.
    """
    require_link(request)
    try:
        session = await _step_session(request, session_id)
    except DBAPIError as exc:
        raise database_failed(
            exc,
            logged=f"step session {session_id} could not be cancelled",
            message="The step session could not be cancelled.",
        ) from None
    return await cancel_work_session(request, session)


async def cancel_work_session(request: Request, session: Any) -> JSONResponse:
    """End the step or the work item *session* works: redact its inputs, pause it and interrupt its turn.

    ``200 {"status": "paused"}``, also when it was paused already.  Every
    cancel interrupts the turn, so a retry stops a turn its first attempt
    did not reach; only the first pauses the session.  A person's case
    agent has no inputs: its turn is interrupted and its session paused,
    and the person's next message wakes it again.
    """
    try:
        await request.app.state.session_store.cancel_case_step(session.id, {"reason": _CANCELLED})
    except SessionNotFoundError:
        # Deleted since the lookup.
        raise unknown_session() from None
    except DBAPIError as exc:
        raise database_failed(
            exc,
            logged=f"session {session.id} could not be cancelled",
            message="The session could not be cancelled.",
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
        raise database_failed(
            exc,
            logged=f"step session {session_id} could not be read",
            message="The step session could not be read.",
        ) from None
    answer = StepSessionStatus(status=session.status, updated_at=session.updated_at)
    return JSONResponse(answer.model_dump(mode="json"))


def _paged(event: Event) -> dict[str, Any]:
    """*event* as a page holds it: its id, type, data, and when it was written, with its offset."""
    item = event.model_dump(mode="json", include={"id", "type", "data"})
    item["created_at"] = None if event.created_at is None else _as_utc(event.created_at).isoformat()
    return item


async def events_page(request: Request, session: Any) -> JSONResponse:
    """A page of *session*'s events after the cursor ``after``, in id order, without ``llm.delta``.

    ``has_more`` says more events follow: Keel reads on with ``after`` set to
    the page's last id.  The route checks the link key and finds the session first.
    """
    after = query_int(request, "after", default=0, low=0, high=_BIGINT_MAX)
    limit = query_int(request, "limit", default=_PAGE_DEFAULT, low=1, high=_PAGE_MAX)
    try:
        # One more than the page holds tells whether more follow.
        read = await request.app.state.session_store.get_events(
            session.id, after=after, limit=limit + 1, exclude_types=[EventType.LLM_DELTA],
        )
    except DBAPIError as exc:
        raise database_unavailable(
            exc,
            logged=f"the events of session {session.id} could not be read",
            message="The session's events could not be read; try again.",
        ) from None
    return JSONResponse({"events": [_paged(event) for event in read[:limit]], "has_more": len(read) > limit})


async def events_stream(request: Request, session: Any) -> EventSourceResponse:
    """*session*'s events after the cursor ``after``, then live, as the session stream sends them when it watches.

    The route checks the link key and finds the session first, and ``after``
    is checked here, before anything streams.  A session at rest between its
    turns does not end the stream: only its time running out, the session
    archived or gone does.  Keel takes up after an end with ``after`` set to
    the last event id it received.
    """
    after = query_int(request, "after", default=0, low=0, high=_BIGINT_MAX)
    return EventSourceResponse(
        session_event_stream(request, request.app.state.session_store, session.id, after=after, watch=True),
    )


@router.get("/keel/step-sessions/{session_id}/events")
async def read_step_session_events(session_id: str, request: Request) -> JSONResponse:
    """A page of a step session's events after the cursor ``after``, without ``llm.delta`` (:func:`events_page`)."""
    require_link(request)
    return await events_page(request, await watched_session(request, session_id, _is_a_step))


@router.get("/keel/step-sessions/{session_id}/events/stream")
async def stream_step_session_events(session_id: str, request: Request) -> EventSourceResponse:
    """A step session's events after the cursor ``after``, then live (:func:`events_stream`).

    A step at rest between its turns does not end the stream.
    """
    require_link(request)
    return await events_stream(request, await watched_session(request, session_id, _is_a_step))


#: The events whose ``data["name"]`` names the tool they call or answer.
_TOOL_EVENTS = frozenset({EventType.TOOL_CALL.value, EventType.TOOL_RESULT.value})


def _out_of_shape(what: str, session_id: UUID, kind: str) -> None:
    """Log that a stored value of the session is out of its shape, by *kind* alone: its text quotes the case."""
    logger.warning("keel-link: %s of session %s is out of its shape (%s)", what, session_id, kind)


async def _todo_list(store: Any, session_id: UUID) -> tuple[list[TodoItem] | None, datetime | None]:
    """The session's todo list as its agent last wrote it, and when; ``(None, None)`` when it never wrote one.

    A stored list out of its shape reads as none, and an item out of its
    shape is left out: one bad row never keeps Keel from a session's progress.
    """
    try:
        listed = await store.last_event(session_id, EventType.TODO_UPDATED)
    except ValidationError as exc:
        _out_of_shape("the todo list", session_id, type(exc).__name__)
        return None, None
    if listed is None:
        return None, None
    stored = listed.data.get("todos")
    if not isinstance(stored, list):
        _out_of_shape("the todo list", session_id, type(stored).__name__)
        return None, None
    items: list[TodoItem] = []
    dropped: str | None = None
    for item in stored:
        try:
            items.append(TodoItem.model_validate(item))
        except ValidationError as exc:
            dropped = type(exc).__name__
    if dropped is not None:
        _out_of_shape("a todo item", session_id, dropped)
    return items, _as_utc(listed.created_at)


async def _last_activity(store: Any, session_id: UUID) -> LastActivity | None:
    """The session's newest event that is not an ``llm.delta``; None when it has none, or it is out of its shape."""
    try:
        event = await store.newest_event(session_id, except_types=[EventType.LLM_DELTA])
    except ValidationError as exc:
        _out_of_shape("the newest event", session_id, type(exc).__name__)
        return None
    if event is None:
        return None
    name = event.data.get("name") if event.type in _TOOL_EVENTS else None
    return LastActivity(
        event_id=event.id,
        type=event.type,
        at=_as_utc(event.created_at),
        tool=name if isinstance(name, str) else None,
    )


async def progress_answer(request: Request, session: Any) -> JSONResponse:
    """*session*'s status, its todo list as its agent last wrote it, and its last activity.

    Only *session* is read: a helper it started keeps its own list.  Stored
    data out of its shape never fails the read: a todo item out of its shape
    is left out, a list out of its shape reads as none, and so does a newest
    event out of its shape.  The route checks the link key and finds the
    session first.
    """
    store = request.app.state.session_store
    try:
        todos, todos_at = await _todo_list(store, session.id)
        last_activity = await _last_activity(store, session.id)
    except DBAPIError as exc:
        raise database_unavailable(
            exc,
            logged=f"the progress of session {session.id} could not be read",
            message="The session's progress could not be read; try again.",
        ) from None
    answer = StepProgress(status=session.status, todos=todos, todos_at=todos_at, last_activity=last_activity)
    return JSONResponse(answer.model_dump(mode="json"))


@router.get("/keel/step-sessions/{session_id}/progress")
async def read_step_session_progress(session_id: str, request: Request) -> JSONResponse:
    """A step session's status, its todo list and its last activity (:func:`progress_answer`).

    Only the step's own session is read: a helper it started keeps its own list.
    """
    require_link(request)
    return await progress_answer(request, await watched_session(request, session_id, _is_a_step))


# ---------------------------------------------------------------------------
# A case's files
# ---------------------------------------------------------------------------


def case_uuid(case_id: str) -> UUID:
    try:
        return UUID(case_id)
    except ValueError:
        raise refused(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid-request", "The case id is not a UUID.",
        ) from None


def checked_path(request: Request, case: UUID, path: str) -> str:
    """*path*, relative to the case's workspace: 422 ``invalid-path`` for one Keel may not name."""
    try:
        relative = case_path(path)
        case_key(request.app.state.settings, case, relative)
    except CaseFileError as exc:
        raise refused(
            status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code, "The path is not one of the case's files.",
        ) from None
    return relative


def _case_path(request: Request, case: UUID) -> str:
    """The ``path`` query parameter, relative to the case's workspace: 422 ``invalid-path`` for one Keel may not name."""
    return checked_path(request, case, request.query_params.get("path") or "")


def _storage_failed(exc: Exception, case: UUID) -> HTTPException:
    """The 503 for a store that would not answer (refused, throttled, unreachable), logged by the error's class name.

    The error's text can quote the object's key: a case's file name.
    """
    logger.warning("keel-link: the files of case %s could not be reached (%s)", case, type(exc).__name__)
    return refused(
        status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable", "The case's files could not be reached; try again.",
    )


def _unknown_file() -> HTTPException:
    return refused(status.HTTP_404_NOT_FOUND, "unknown-file", "No such file in the case's workspace.")


def _too_large(limit: int) -> HTTPException:
    return refused(status.HTTP_413_CONTENT_TOO_LARGE, "too-large", f"The file is larger than {limit} bytes.")


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
    case = case_uuid(case_id)
    state = request.app.state
    try:
        files = await list_case_files(state.storage, state.settings, case)
    except (ClientError, BotoCoreError) as exc:
        raise _storage_failed(exc, case) from None
    workspace = case_workspace(state.storage, state.settings, case)
    try:
        # Every open row under the case's workspace, by its location's prefix: one query, however many files.
        documents = await open_under(state.session_factory, workspace.location("")) if files else []
    except DBAPIError as exc:
        raise database_failed(
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


def _file_digest(path: Path) -> str:
    """The SHA-256 of the file at *path*, read a piece at a time."""
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _opened_and_removed(path: Path) -> BinaryIO:
    """*path* opened, and removed, so nothing is left behind however its reader ends."""
    source = open(path, "rb")
    try:
        path.unlink()
    except BaseException:
        source.close()
        raise
    return source


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
    The bytes are kept under that digest before it is sent: with no version
    kept the answer is 503, and no digest has left.
    """
    require_link(request)
    case = case_uuid(case_id)
    relative = _case_path(request, case)
    state = request.app.state
    limit = state.settings.keel.max_file_bytes
    target = await asyncio.to_thread(_scratch_file)
    try:
        size = await download_case_file(state.storage, state.settings, case, relative, target, limit=limit)
        digest = await asyncio.to_thread(_file_digest, target)
        await case_versions(state.settings, case).keep_file(state.storage, digest, target)
        source = await asyncio.to_thread(_opened_and_removed, target)
    except FileNotFoundError:
        raise _unknown_file() from None
    except TooLarge:
        raise _too_large(limit) from None
    except VersionNotKept as exc:
        raise _storage_failed(exc.__cause__ or exc, case) from None
    except (ClientError, BotoCoreError) as exc:
        raise _storage_failed(exc, case) from None
    finally:
        target.unlink(missing_ok=True)
    return StreamingResponse(
        _streamed(source),
        media_type="application/octet-stream",
        headers={"X-Content-SHA256": digest, "Content-Length": str(size)},
    )


async def body_within(request: Request, limit: int) -> bytes:
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
    case = case_uuid(case_id)
    relative = _case_path(request, case)
    data = await body_within(request, request.app.state.settings.keel.max_file_bytes)
    return await write_case_file_answer(request, case, relative, data)


async def write_case_file_answer(request: Request, case: UUID, relative: str, data: bytes) -> JSONResponse:
    """Write *data* at *relative* in *case*'s workspace, and answer with the file's Keel value and size.

    A file open in the editor is refused, 409 ``file-open``.  The bytes are
    kept under their digest before the file is written, so the digest in the
    answer names bytes the runtime holds: with no version kept, or a store
    that would not answer, the answer is 503 and the file is not written.
    """
    state = request.app.state
    location = case_workspace(state.storage, state.settings, case).location(relative)
    try:
        open_now = await open_at(state.session_factory, location)
    except DBAPIError as exc:
        raise database_failed(
            exc,
            logged=f"the open documents of case {case} could not be read",
            message="The file could not be written.",
        ) from None
    if open_now is not None:
        raise refused(status.HTTP_409_CONFLICT, "file-open", "The file is open in the editor.")
    value = await asyncio.to_thread(file_value, case, relative, data)
    versions = case_versions(state.settings, case)
    try:
        # Keel may write a case's files before its first step session makes the bucket.
        await state.storage.create_bucket(versions.bucket)
        await versions.keep(state.storage, value["sha256"], data)
        await write_case_file(state.storage, state.settings, case, relative, data)
    except VersionNotKept as exc:
        raise _storage_failed(exc.__cause__ or exc, case) from None
    except (ClientError, BotoCoreError) as exc:
        raise _storage_failed(exc, case) from None
    return JSONResponse({**value, "size": len(data)})


def _unknown_version() -> HTTPException:
    return refused(status.HTTP_404_NOT_FOUND, "unknown-version", "No such version of the case's files.")


@router.get("/keel/cases/{case_id}/versions/{sha256}")
async def read_version_of_case(case_id: str, sha256: str, request: Request) -> StreamingResponse:
    """A version of a file of the case, by its hash, streamed as a file's read is, with the hash in ``X-Content-SHA256``.

    Past the size Keel may write it is 413 ``too-large``.
    """
    require_link(request)
    case = case_uuid(case_id)
    if not SHA256_HEX.fullmatch(sha256):
        raise refused(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid-request", "The hash is not a SHA-256 digest.")
    state = request.app.state
    limit = state.settings.keel.max_file_bytes
    target = await asyncio.to_thread(_scratch_file)
    try:
        size = await case_versions(state.settings, case).download(state.storage, sha256, target, limit=limit)
        source = await asyncio.to_thread(_opened_and_removed, target)
    except KeyError:
        raise _unknown_version() from None
    except TooLarge:
        raise _too_large(limit) from None
    except (ClientError, BotoCoreError) as exc:
        raise _storage_failed(exc, case) from None
    finally:
        target.unlink(missing_ok=True)
    return StreamingResponse(
        _streamed(source),
        media_type="application/octet-stream",
        headers={"X-Content-SHA256": sha256, "Content-Length": str(size)},
    )


@router.post("/keel/cases/{case_id}/versions/missing")
async def versions_missing_of_case(case_id: str, request: Request) -> JSONResponse:
    """Which of the hashes Keel names the case holds no version of, each once, in the order asked."""
    require_link(request)
    case = case_uuid(case_id)
    asked = await read_body(request, MissingVersions)
    state = request.app.state
    try:
        gone = await case_versions(state.settings, case).missing(state.storage, asked.sha256)
    except (ClientError, BotoCoreError) as exc:
        raise _storage_failed(exc, case) from None
    return JSONResponse({"missing": gone})


@router.delete("/keel/cases/{case_id}/versions")
async def delete_versions_of_case(case_id: str, request: Request) -> JSONResponse:
    """Delete every version the case holds, and no file of its workspace: how many.  Keel asks once the case's retention ends."""
    require_link(request)
    case = case_uuid(case_id)
    state = request.app.state
    try:
        deleted = await case_versions(state.settings, case).delete_all(state.storage)
    except (ClientError, BotoCoreError) as exc:
        raise _storage_failed(exc, case) from None
    return JSONResponse({"deleted": deleted})


async def org_member_name(request: Request, org_id: UUID, user_id: UUID) -> str | None:
    """The display name of *user_id* when they are a user of *org_id*, a home user or a member; None for anyone else.

    A database failure is raised as it is, for the route to answer.
    """
    async with request.app.state.session_factory() as db:
        return (
            await db.execute(select(User.display_name).where(User.id == user_id, belongs_to(org_id)))
        ).scalar_one_or_none()


async def _member_name(request: Request, org_id: UUID, user_id: UUID) -> str:
    """The display name of *user_id*, a user of *org_id*: 404 ``unknown-user`` for anyone else."""
    try:
        name = await org_member_name(request, org_id, user_id)
    except DBAPIError as exc:
        raise database_failed(
            exc, logged=f"user {user_id} could not be read", message="The editor could not be opened.",
        ) from None
    if name is None:
        raise refused(status.HTTP_404_NOT_FOUND, "unknown-user", "No such user in this org.")
    return name


@router.post("/keel/cases/{case_id}/editor")
async def open_case_document(case_id: str, request: Request) -> JSONResponse:
    """One person's editor config for a document of the case's workspace, with the access Keel gives them.

    The person is the runtime user, under their display name, as when they
    open a file in an agent's chat.  Everyone who opens the file, and any
    step session of the case that has it open, shares its one editing
    session.  A kind the editor only converts to view opens read-only,
    whatever the access.

    The case's org is recorded first, when none is: a case recorded under
    another org is 404 ``unknown-file``, as a file that is not there.  The
    agents Keel names are offered to mention where an agent answers a
    comment: the runtime has agent editing, and the editor edits a
    document an agent edits.
    """
    require_link(request)
    case = case_uuid(case_id)
    body = await read_body(request, CaseEditorRequest)
    state = request.app.state
    office = state.settings.office
    if not office.enabled:
        raise refused(status.HTTP_503_SERVICE_UNAVAILABLE, "office-off", "No document server is configured.")
    relative = checked_path(request, case, body.path)
    kind = office_kind(relative)
    if kind is None:
        raise refused(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "not-a-document", "The file is not an office document.",
        )
    workspace = case_workspace(state.storage, state.settings, case)
    try:
        recorded = await record_case_org(state.storage, state.settings, case, body.org_id)
        # Another org's case is not told apart from a file that is not there.
        found = await workspace.stat(relative) if recorded == body.org_id else None
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
        raise refused(
            status.HTTP_409_CONFLICT, "document-closing", "The document is still being saved; try again in a moment.",
        ) from None
    except DBAPIError as exc:
        raise database_failed(
            exc,
            logged=f"a document of case {case} could not be opened",
            message="The editor could not be opened.",
        ) from None
    access = body.access if kind.editable else "view"
    config = build_editor_config(
        settings=office, row_id=document.id, doc_key=document.doc_key, path=relative, kind=kind,
        user_id=editor_id, user_name=name, access=access, theme=body.theme,
    )
    offered = office.agent_editing and access != "view" and kind.file_type in AGENT_KINDS
    return JSONResponse({
        "document_server_url": office.public_url.rstrip("/"),
        "config": config,
        "mentionable_users": [mentionable_agent(agent.id, agent.name) for agent in body.mentionable] if offered else [],
    })


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
        raise refused(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid-request", "The org id is not a UUID.",
        ) from None


async def _firebase_project(request: Request, org_id: UUID) -> str | None:
    """The Firebase project the org signs in with, None when it has none: 503 ``unavailable`` when it cannot be read.

    A failure is no "no Firebase": a password account created in an org
    that signs in with Firebase would have no way in.
    """
    cache = getattr(request.app.state, "firebase_config_cache", None)
    try:
        config = await project_firebase(cache, str(org_id))
    except FirebaseConfigUnavailable as unavailable:
        logger.warning(
            "keel-link: the Firebase config of org %s could not be read (%s)",
            org_id, type(unavailable.__cause__).__name__,
        )
        raise refused(
            status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable", "The org's sign-in could not be read; try again.",
        ) from None
    return None if config is None else config.firebase_project_id


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
        raise refused(status_code, exc.code, message) from None
    except DBAPIError as exc:
        raise database_failed(
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
