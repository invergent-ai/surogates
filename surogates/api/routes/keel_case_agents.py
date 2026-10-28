"""Keel's routes for a person's case agent: through ``keel-link``, Keel starts
its session, gives it the person's messages, reads its status and its events,
by page and live, and stops its turn.

A case agent is one person's agent on one case (:mod:`surogates.keel.case_agents`).
Its session is on ``api``, owned by the org's ``keel-case-agent`` account,
never by the person, with the case's ``case:`` memory boundary and a scratch
workspace of its own.  A person waits on it, so its wakes queue beside
people's, ahead of case work.  The same link key, errors and keys as the step
routes (:mod:`surogates.api.routes.keel`), whose helpers these routes share.

Mounted only on the keel-link app, never on the public API.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import DBAPIError
from sse_starlette.sse import EventSourceResponse

from surogates.api.routes.keel import (
    agent_id_of,
    cancel_work_session,
    create_or_find,
    database_failed,
    events_page,
    events_stream,
    note_case_org,
    org_member_name,
    queue_session,
    read_body,
    refuse_a_reruns_folder,
    refused,
    require_link,
    resolve_agent,
    session_where,
    unknown_session,
    watched_session,
)
from surogates.channels.constants import API_CHANNEL
from surogates.channels.memory_boundary import CASE_BOUNDARY_PREFIX
from surogates.keel.case_agents import CaseAgentRef, case_agent_of, is_case_agent
from surogates.keel.steps import PERSON_MARKER
from surogates.session.provisioning import create_agent_session
from surogates.session.store import SessionNotFoundError
from surogates.tenant.auth.service_account import ServiceAccountStore

router = APIRouter()


class CaseAgentSessionRequest(BaseModel):
    """Keel's request to start one person's case agent on one case.  Keys this runtime does not know are ignored."""

    model_config = ConfigDict(extra="ignore")

    org_id: UUID
    case_id: UUID
    case_number: str = Field(min_length=1, max_length=200)
    # The user of the org whose agent this is.
    person_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    # Keel's id for the session: the runtime creates it with exactly this one.
    session_id: UUID


class CaseAgentMessage(BaseModel):
    """A person's message to their case agent.  A key this route does not know is refused."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=20000)
    idempotency_key: str = Field(min_length=1, max_length=200)


async def _case_agent_session(request: Request, session_id: str) -> Any:
    """The case agent session *session_id* names: 404 ``unknown-session`` for any other id.

    A step's or an item's session works on the case, and a helper of the
    case agent's for it: none is the case agent's.
    """
    return await session_where(request, session_id, is_case_agent)


@router.post("/keel/case-agent-sessions")
async def create_case_agent_session(request: Request) -> JSONResponse:
    """Start one person's case agent on a case: 201 made, or 200 found by its key.

    Nothing is written to the session or queued: the person's first message wakes it.
    """
    require_link(request)
    body = await read_body(request, CaseAgentSessionRequest)
    agent_id = agent_id_of(request)
    await resolve_agent(request, agent_id, body.org_id)
    await refuse_a_reruns_folder(request, body.case_id)
    ref = CaseAgentRef(case_id=body.case_id, case_number=body.case_number, person_id=body.person_id)

    def this_agent(existing: Any) -> bool:
        held = case_agent_of(existing)
        return (
            existing.agent_id == agent_id
            and held is not None
            and held.case_id == ref.case_id
            and held.person_id == ref.person_id
        )

    async def make() -> Any:
        account_id = await ServiceAccountStore(request.app.state.session_factory).case_agent_account(body.org_id)
        return await create_agent_session(
            store=request.app.state.session_store,
            storage=request.app.state.storage,
            settings=request.app.state.settings,
            org_id=body.org_id,
            user_id=None,
            agent_id=agent_id,
            channel=API_CHANNEL,
            config={"memory_boundary": f"{CASE_BOUNDARY_PREFIX}{ref.case_id}"},
            service_account_id=account_id,
            idempotency_key=body.idempotency_key,
            session_id=body.session_id,
            case_agent=ref.model_dump(mode="json"),
        )

    async def start(_session: Any) -> None:
        # Nothing to write: the person's first message wakes the session.
        return None

    try:
        if await org_member_name(request, body.org_id, body.person_id) is None:
            raise refused(status.HTTP_409_CONFLICT, "not-a-member", "The person is not a user of this org.")
        session, created = await create_or_find(request, body, matches=this_agent, make=make, start=start)
    except DBAPIError as exc:
        raise database_failed(
            exc,
            logged=f"the case agent session of case {ref.case_id} could not be written",
            message="The case agent session could not be created.",
        ) from None
    await note_case_org(request, ref.case_id, body.org_id)
    return JSONResponse(
        {"session_id": str(session.id)},
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@router.get("/keel/case-agent-sessions/{session_id}")
async def read_case_agent_session(session_id: str, request: Request) -> JSONResponse:
    """A case agent session's id and status."""
    require_link(request)
    try:
        session = await _case_agent_session(request, session_id)
    except DBAPIError as exc:
        raise database_failed(
            exc,
            logged=f"case agent session {session_id} could not be read",
            message="The case agent session could not be read.",
        ) from None
    return JSONResponse({"session_id": str(session.id), "status": session.status})


@router.post("/keel/case-agent-sessions/{session_id}/cancel")
async def cancel_case_agent_session(session_id: str, request: Request) -> JSONResponse:
    """Stop a case agent's turn and pause its session; 200, also when paused already.

    The person's next message wakes it again.
    """
    require_link(request)
    try:
        session = await _case_agent_session(request, session_id)
    except DBAPIError as exc:
        raise database_failed(
            exc,
            logged=f"case agent session {session_id} could not be cancelled",
            message="The case agent session could not be cancelled.",
        ) from None
    return await cancel_work_session(request, session)


@router.post("/keel/case-agent-sessions/{session_id}/messages")
async def send_case_agent_message(session_id: str, request: Request) -> JSONResponse:
    """Give a case agent its person's message, once per key, and wake it: 202 ``{"status": "queued"}``.

    The message is a ``user.message`` marked ``keel_person_message``, which
    the session reads as it reads a person's typed message and never runs
    as a command.  A session at rest resumes before it, and the session is
    queued, beside people's work.  A repeated key writes, wakes and queues
    nothing, and answers the same.
    """
    require_link(request)
    try:
        session = await _case_agent_session(request, session_id)
        body = await read_body(request, CaseAgentMessage)
        written = await request.app.state.session_store.emit_keyed_synthetic_message(
            session.id, content=body.text, synthetic=PERSON_MARKER, key=body.idempotency_key, resume=True,
        )
    except SessionNotFoundError:
        # Deleted since the lookup above.
        raise unknown_session() from None
    except DBAPIError as exc:
        raise database_failed(
            exc,
            logged=f"the message to case agent session {session_id} could not be written",
            message="The message could not be delivered.",
        ) from None
    if written is not None:
        await queue_session(request, session)
    return JSONResponse({"status": "queued"}, status_code=status.HTTP_202_ACCEPTED)


@router.get("/keel/case-agent-sessions/{session_id}/events")
async def read_case_agent_session_events(session_id: str, request: Request) -> JSONResponse:
    """A page of a case agent session's events after the cursor ``after``, without ``llm.delta`` (:func:`events_page`)."""
    require_link(request)
    return await events_page(request, await watched_session(request, session_id, is_case_agent))


@router.get("/keel/case-agent-sessions/{session_id}/events/stream")
async def stream_case_agent_session_events(session_id: str, request: Request) -> EventSourceResponse:
    """A case agent session's events after the cursor ``after``, then live (:func:`events_stream`)."""
    require_link(request)
    return await events_stream(request, await watched_session(request, session_id, is_case_agent))
