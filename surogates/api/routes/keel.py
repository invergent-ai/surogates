"""Keel's routes: through ``keel-link``, Keel starts an agent's step session,
answers it or asks it for a revision, cancels it, and reads its status.

Mounted only on the keel-link app (:mod:`surogates.api.keel_link`), never
on the public API.  Every route checks the link key with
:func:`require_link`, and loads sessions directly: no tenant, no JWT.

An error is ``HTTPException(status, {"code", "message"})``, which answers
``{"detail": {"code", "message"}}``.  Its message names no case data, and
neither does the log: a case's inputs are a client's.
"""

from __future__ import annotations

import hmac
import json
import logging
from datetime import datetime
from typing import Any, Literal, TypeVar
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.exc import DBAPIError, IntegrityError

from surogates.channels.constants import API_CHANNEL, session_queue_priority
from surogates.channels.memory_boundary import CASE_BOUNDARY_PREFIX
from surogates.config import INTERRUPT_CHANNEL_PREFIX, enqueue_session
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
from surogates.session.provisioning import create_agent_session
from surogates.session.store import SessionNotFoundError, StepFinishedError
from surogates.tenant.auth.service_account import ServiceAccountStore

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
