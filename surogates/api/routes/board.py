"""REST read endpoint for coordination boards.

Spec §12: ``GET /v1/sessions/{session_id}/board`` returns the active
notes and the consolidated render of the session's coordination group.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from surogates.board.render import render_board
from surogates.board.store import BoardStore
from surogates.config import get_board_settings
from surogates.session.store import SessionNotFoundError, SessionStore
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

router = APIRouter()


class BoardNoteOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    seq: int
    writer_label: str
    type: str
    content: str
    status: str
    ref: dict[str, Any] | None = None
    created_at: datetime
    expires_at: datetime | None = None


class BoardResponse(BaseModel):
    group_id: UUID
    notes: list[BoardNoteOut]
    render: str


def _get_session_store(request: Request) -> SessionStore:
    store: SessionStore | None = getattr(
        request.app.state, "session_store", None,
    )
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session store not available.",
        )
    return store


@router.get("/sessions/{session_id}/board", response_model=BoardResponse)
async def get_session_board(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> BoardResponse:
    """Current board of the session's coordination group.

    404 when the session does not exist for this tenant or is not a
    coordination-group member.
    """
    store = _get_session_store(request)
    try:
        session = await store.get_session(session_id)
    except SessionNotFoundError:
        session = None
    if session is None or not tenant.owns_session(session.org_id, session_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )
    raw_group = (session.config or {}).get("context_group_id")
    if not raw_group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session is not a coordination-group member.",
        )
    group_id = UUID(str(raw_group))

    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session factory not available.",
        )

    board = BoardStore(session_factory)
    notes = await board.active_notes(group_id)
    settings = get_board_settings()
    render = render_board(
        notes,
        max_tokens=settings.read_tool_window_tokens,
        now=datetime.now(timezone.utc),
        header="[Board — consolidated current state]",
        footer="",
    )
    return BoardResponse(
        group_id=group_id,
        notes=[BoardNoteOut.model_validate(n) for n in notes],
        render=render,
    )
