"""Worker completion notification for coordinator sessions.

When a worker session (``channel="worker"``) completes or fails, this
module emits a notification event into the parent session's event log
and re-enqueues the parent so it wakes up to process the result.

Called from :meth:`AgentHarness._complete_session` when
``session.parent_id`` is set.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from surogates.config import enqueue_session
from surogates.session.events import EventType

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from surogates.session.store import SessionStore

logger = logging.getLogger(__name__)

# Maximum characters of the worker's final response to embed in the
# notification event.
_MAX_RESULT_CHARS: int = 10_000


async def notify_parent_on_completion(
    *,
    session_store: SessionStore,
    worker_session_id: UUID,
    parent_session_id: UUID,
    agent_id: str,
    redis: Redis | None = None,
) -> None:
    """Emit a ``WORKER_COMPLETE`` event into the parent session and re-enqueue it.

    Extracts the last ``LLM_RESPONSE`` content from the worker's event log
    and includes it (truncated) in the notification so the coordinator LLM
    can see what the worker produced.  ``agent_id`` is the parent's agent id
    (the child inherits it, so either value works) and selects the
    per-agent work queue.
    """
    try:
        from surogates.harness.message_utils import extract_final_response

        events = await session_store.get_events(worker_session_id)
        final_response = extract_final_response(events)

        await session_store.emit_event(
            parent_session_id,
            EventType.WORKER_COMPLETE,
            {
                "worker_id": str(worker_session_id),
                "result": final_response[:_MAX_RESULT_CHARS],
            },
        )

        # Re-enqueue the parent so it wakes up.
        if redis is not None:
            await enqueue_session(redis, agent_id, parent_session_id)

    except Exception:
        logger.exception(
            "Failed to notify parent %s of worker %s completion",
            parent_session_id,
            worker_session_id,
        )


async def notify_parent_on_failure(
    *,
    session_store: SessionStore,
    worker_session_id: UUID,
    parent_session_id: UUID,
    agent_id: str,
    error: str,
    redis: Redis | None = None,
) -> None:
    """Emit a ``WORKER_FAILED`` event into the parent session and re-enqueue it.

    ``agent_id`` selects the per-agent work queue so the parent wakes on the
    same worker that owns its agent.
    """
    try:
        await session_store.emit_event(
            parent_session_id,
            EventType.WORKER_FAILED,
            {
                "worker_id": str(worker_session_id),
                "error": error[:2000],
            },
        )

        if redis is not None:
            await enqueue_session(redis, agent_id, parent_session_id)

    except Exception:
        logger.exception(
            "Failed to notify parent %s of worker %s failure",
            parent_session_id,
            worker_session_id,
        )


