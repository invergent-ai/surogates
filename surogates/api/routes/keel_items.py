"""Keel's routes for an agent's session on a work item: through ``keel-link``,
Keel starts it, sends it messages, reads its status, its events, by page and
live, and its progress, and cancels it.

A case gives an agent a work item as it gives a person one: a step, a vote
or a raised hand (:mod:`surogates.keel.items`).  Its session runs as a
step's does: on ``api``, owned by the case type's account, inside the case's
boundaries, behind people in the queue.  The same link key, errors and keys
as the step routes (:mod:`surogates.api.routes.keel`), whose helpers these
routes share.

Mounted only on the keel-link app, never on the public API.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import DBAPIError
from sse_starlette.sse import EventSourceResponse

from surogates.api.routes.keel import (
    SenderName,
    agent_id_of,
    cancel_work_session,
    create_case_session,
    create_or_find,
    database_failed,
    events_page,
    events_stream,
    kick_off,
    note_case_org,
    progress_answer,
    queue_session,
    read_body,
    require_link,
    resolve_agent,
    screen,
    session_where,
    unknown_session,
    watched_session,
)
from surogates.keel.items import (
    ItemContract,
    is_case_item,
    item_contract_of,
    render_item_kickoff,
    render_item_message,
)
from surogates.keel.run_facts import bundle_version_in
from surogates.keel.steps import ITEM_KICKOFF_MARKER, MESSAGE_MARKER, sleeps_through_a_plain_message
from surogates.session.store import SessionNotFoundError, StepFinishedError

router = APIRouter()

#: What the injection screen reads of an item: the case's data, and a raised hand's question.
_SCREENED = {"inputs", "question", "choices", "evidence"}


class ItemSessionRequest(BaseModel):
    """Keel's request to start an agent's session on one work item.  Keys this runtime does not know are ignored."""

    model_config = ConfigDict(extra="ignore")

    org_id: UUID
    case_type: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)
    # Keel's id for the session: the runtime creates it with exactly this one.
    session_id: UUID
    contract: ItemContract

    @model_validator(mode="after")
    def _one_case_type(self) -> ItemSessionRequest:
        # The session's owning account is this case type's: it must be the contract's.
        if self.case_type != self.contract.case_type:
            raise ValueError("case_type differs from the contract's")
        return self


class ItemMessage(BaseModel):
    """Keel's plain message to an item, from a person or another agent working on the case.
    A key this route does not know is refused."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=20000)
    sender: SenderName | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)


async def _item_session(request: Request, session_id: str) -> Any:
    """The item session *session_id* names: 404 ``unknown-session`` for any other id.

    A child of the item's session works on the case, and a step's session on
    another contract: neither is the item's.
    """
    return await session_where(request, session_id, is_case_item)


@router.post("/keel/item-sessions")
async def create_item_session(request: Request) -> JSONResponse:
    """Start the agent's session on one work item of a case: 201 made, or 200 found by its key."""
    require_link(request)
    body = await read_body(request, ItemSessionRequest)
    agent_id = agent_id_of(request)
    agent = await resolve_agent(request, agent_id, body.org_id)
    contract = body.contract
    screen(contract.model_dump(mode="json", include=_SCREENED), case_id=contract.case_id)

    def this_item(existing: Any) -> bool:
        held = item_contract_of(existing)
        return (
            existing.agent_id == agent_id
            and held is not None
            and held.case_id == contract.case_id
            and held.item_id == contract.item_id
        )

    async def start(session: Any) -> None:
        await kick_off(
            request,
            session,
            content=render_item_kickoff(contract),
            synthetic=ITEM_KICKOFF_MARKER,
            key=body.idempotency_key,
        )

    try:
        session, created = await create_or_find(
            request,
            body,
            matches=this_item,
            make=lambda: create_case_session(request, body, agent_id, case_item=contract.model_dump(mode="json")),
            start=start,
        )
    except DBAPIError as exc:
        raise database_failed(
            exc,
            logged=f"the item session of case {contract.case_id} could not be written",
            message="The item session could not be created.",
        ) from None
    await note_case_org(request, contract.case_id, body.org_id)
    return JSONResponse(
        {"session_id": str(session.id), "bundle_version": bundle_version_in(agent.get("bundle_version"))},
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@router.get("/keel/item-sessions/{session_id}")
async def read_item_session(session_id: str, request: Request) -> JSONResponse:
    """An item session's id and status."""
    require_link(request)
    try:
        session = await _item_session(request, session_id)
    except DBAPIError as exc:
        raise database_failed(
            exc,
            logged=f"item session {session_id} could not be read",
            message="The item session could not be read.",
        ) from None
    return JSONResponse({"session_id": str(session.id), "status": session.status})


@router.post("/keel/item-sessions/{session_id}/cancel")
async def cancel_item_session(session_id: str, request: Request) -> JSONResponse:
    """End an item: redact its inputs, pause its session and interrupt its turn; 200, also when paused already."""
    require_link(request)
    try:
        session = await _item_session(request, session_id)
    except DBAPIError as exc:
        raise database_failed(
            exc,
            logged=f"item session {session_id} could not be cancelled",
            message="The item session could not be cancelled.",
        ) from None
    return await cancel_work_session(request, session)


@router.post("/keel/item-sessions/{session_id}/messages")
async def send_item_message(session_id: str, request: Request) -> JSONResponse:
    """Give an item Keel's plain message, once per key: 202 ``{"status": "queued" | "resting"}``.

    The message wakes an item whose session is at rest, and is queued with
    it: ``queued``.  An item that rests on its report, or is over, keeps the
    message unread, and nothing is woken or queued: ``resting``.  A message
    never gives the item back, so a report's mark stays.  A repeated key
    writes nothing, and answers as the item stands.
    """
    require_link(request)
    store = request.app.state.session_store
    queued = False
    try:
        session = await _item_session(request, session_id)
        body = await read_body(request, ItemMessage)
        message = {
            "content": render_item_message(body.text, body.sender),
            "synthetic": MESSAGE_MARKER,
            "key": body.idempotency_key,
        }
        try:
            written = await store.emit_keyed_synthetic_message(session.id, **message, resume=True, keep_report=True)
        except StepFinishedError:
            # Over: the message is kept, unread, and wakes nothing.
            await store.emit_keyed_synthetic_message(session.id, **message, resume=False)
            resting = True
        else:
            # A repeat writes nothing: the row read above says whether the item rests.
            resting = written.resting if written is not None else sleeps_through_a_plain_message(session.config)
            queued = written is not None and not resting
    except SessionNotFoundError:
        # Deleted since the lookup above.
        raise unknown_session() from None
    except DBAPIError as exc:
        raise database_failed(
            exc,
            logged=f"the message to item session {session_id} could not be written",
            message="The message could not be delivered.",
        ) from None
    if queued:
        await queue_session(request, session)
    return JSONResponse({"status": "resting" if resting else "queued"}, status_code=status.HTTP_202_ACCEPTED)


@router.get("/keel/item-sessions/{session_id}/events")
async def read_item_session_events(session_id: str, request: Request) -> JSONResponse:
    """A page of an item session's events after the cursor ``after``, without ``llm.delta`` (:func:`events_page`)."""
    require_link(request)
    return await events_page(request, await watched_session(request, session_id, is_case_item))


@router.get("/keel/item-sessions/{session_id}/events/stream")
async def stream_item_session_events(session_id: str, request: Request) -> EventSourceResponse:
    """An item session's events after the cursor ``after``, then live (:func:`events_stream`)."""
    require_link(request)
    return await events_stream(request, await watched_session(request, session_id, is_case_item))


@router.get("/keel/item-sessions/{session_id}/progress")
async def read_item_session_progress(session_id: str, request: Request) -> JSONResponse:
    """An item session's status, its todo list and its last activity (:func:`progress_answer`).

    Only the item's own session is read: a helper it started keeps its own list.
    """
    require_link(request)
    return await progress_answer(request, await watched_session(request, session_id, is_case_item))
