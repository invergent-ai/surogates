"""Keel's routes for an agent's session on a work item: through ``keel-link``,
Keel starts it, reads its status and cancels it.

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

from surogates.api.routes.keel import (
    agent_id_of,
    cancel_work_session,
    create_case_session,
    create_or_find,
    database_failed,
    kick_off,
    read_body,
    require_link,
    resolve_agent,
    screen,
    session_where,
)
from surogates.keel.items import ItemContract, is_case_item, item_contract_of, render_item_kickoff
from surogates.keel.steps import ITEM_KICKOFF_MARKER

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
    await resolve_agent(request, agent_id, body.org_id)
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
    return JSONResponse(
        {"session_id": str(session.id)},
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
