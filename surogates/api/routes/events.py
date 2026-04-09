"""SSE event streaming and polling endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from surogates.session.models import Event
from surogates.session.store import SessionNotFoundError, SessionStore
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter()

# Terminal session statuses -- when the session enters one of these states the
# SSE stream sends a final event and closes.
_TERMINAL_STATUSES = frozenset({"completed", "failed", "archived"})

# Maximum time (seconds) an SSE connection stays open before the server
# closes it gracefully.  Clients are expected to reconnect.
_MAX_STREAM_DURATION = 300

# Interval (seconds) between polls when no new events are found.
_POLL_INTERVAL = 1.0


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class PollEventsResponse(BaseModel):
    events: list[Event]
    has_more: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_session_store(request: Request) -> SessionStore:
    store: SessionStore | None = getattr(request.app.state, "session_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session store not available.",
        )
    return store


async def _verify_session_access(
    store: SessionStore, session_id: UUID, tenant: TenantContext
) -> None:
    """Raise 404 if the session does not exist or does not belong to the tenant."""
    try:
        session = await store.get_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )
    if session.org_id != tenant.org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )


# ---------------------------------------------------------------------------
# SSE streaming endpoint
# ---------------------------------------------------------------------------


@router.get("/sessions/{session_id}/events")
async def stream_events(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    after: int = 0,
) -> EventSourceResponse:
    """Stream session events via Server-Sent Events.

    The client provides ``after`` (the last event ID it received) and the
    server yields all subsequent events as they appear.  When the session
    reaches a terminal status, a ``session.done`` event is emitted and the
    stream closes.
    """
    store = _get_session_store(request)
    await _verify_session_access(store, session_id, tenant)

    async def event_generator():  # noqa: ANN202
        cursor = after
        elapsed = 0.0

        while elapsed < _MAX_STREAM_DURATION:
            # Check if client disconnected.
            if await request.is_disconnected():
                return

            events = await store.get_events(session_id, after=cursor, limit=50)

            for event in events:
                yield {
                    "id": str(event.id),
                    "event": event.type,
                    "data": json.dumps(event.data, default=str),
                }
                if event.id is not None:
                    cursor = event.id

            if not events:
                # No new events -- check if the session has terminated.
                try:
                    session = await store.get_session(session_id)
                except SessionNotFoundError:
                    yield {
                        "event": "session.done",
                        "data": json.dumps({"reason": "session_not_found"}),
                    }
                    return

                if session.status in _TERMINAL_STATUSES:
                    yield {
                        "event": "session.done",
                        "data": json.dumps(
                            {"reason": session.status, "status": session.status}
                        ),
                    }
                    return

                await asyncio.sleep(_POLL_INTERVAL)
                elapsed += _POLL_INTERVAL

        # Stream duration exceeded -- close gracefully.
        yield {
            "event": "stream.timeout",
            "data": json.dumps({"reason": "max_duration_exceeded"}),
        }

    return EventSourceResponse(event_generator())


# ---------------------------------------------------------------------------
# One-shot polling endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/sessions/{session_id}/events/poll",
    response_model=PollEventsResponse,
)
async def poll_events(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    after: int = 0,
    limit: int = 50,
) -> PollEventsResponse:
    """Fetch events in a single request (non-streaming alternative to SSE)."""
    store = _get_session_store(request)
    await _verify_session_access(store, session_id, tenant)

    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200

    # Fetch one extra to determine ``has_more``.
    events = await store.get_events(session_id, after=after, limit=limit + 1)

    has_more = len(events) > limit
    if has_more:
        events = events[:limit]

    return PollEventsResponse(events=events, has_more=has_more)
