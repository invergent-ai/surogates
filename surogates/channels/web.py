"""Web channel -- REST API + SSE.

The web channel is the primary user-facing interface.  Unlike messaging
platforms (Slack, Teams, Telegram) it does **not** need a separate adapter
because the FastAPI routes *are* the channel:

* ``POST /v1/sessions/{id}/messages`` -- send a message into a session.
* ``GET  /v1/sessions/{id}/events``   -- SSE stream of session events.
* ``POST /v1/auth/login``             -- JWT authentication.

This module provides helper functions that the API route handlers call to
bridge between the HTTP layer and the durable delivery pipeline.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from surogates.channels.delivery import DeliveryService
from surogates.session.models import Event

__all__ = [
    "format_sse_event",
    "notify_web_clients",
]

logger = logging.getLogger(__name__)


async def notify_web_clients(
    delivery_service: DeliveryService,
    session_id: UUID,
    event_id: int,
    event_data: dict[str, Any],
) -> None:
    """Enqueue a web delivery and nudge live SSE subscribers.

    Called by the harness (or any code path that produces events destined
    for a web-connected browser) after the event has been persisted to the
    ``events`` table.  The flow is:

    1. Insert a row into ``delivery_outbox`` with ``channel='web'``.
       The SSE route does **not** read from the outbox directly -- it
       reads from the ``events`` table -- but the outbox row serves as a
       durable record and enables future features like push notifications.
    2. Publish a Redis pub/sub nudge so that any SSE connection currently
       blocked on ``subscribe()`` wakes up and fetches the new event(s).
    """
    await delivery_service.enqueue(
        session_id=session_id,
        event_id=event_id,
        channel="web",
        destination={"session_id": str(session_id)},
        payload=event_data,
    )
    await delivery_service.nudge(session_id)


def format_sse_event(event: Event) -> dict[str, Any]:
    """Format a session :class:`Event` for SSE transmission.

    Returns a dict with the structure expected by ``sse-starlette``'s
    ``EventSourceResponse``::

        {
            "event": "llm.response",
            "id":    "42",
            "data":  "{\"content\": \"Hello!\"}"
        }

    The ``id`` field is set to the event's database ID so that clients can
    resume a broken connection with ``Last-Event-ID``.
    """
    import json

    return {
        "event": event.type,
        "id": str(event.id) if event.id is not None else "",
        "data": json.dumps(event.data, default=str),
    }
