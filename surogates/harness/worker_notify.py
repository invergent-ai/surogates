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
    task_id: UUID | None = None,
    session_factory: Any | None = None,
) -> None:
    """Emit a ``WORKER_COMPLETE`` event into the parent session and re-enqueue it.

    Extracts the last ``LLM_RESPONSE`` content from the worker's event log
    and includes it (truncated) in the notification so the coordinator LLM
    can see what the worker produced.  ``agent_id`` is the parent's agent id
    (the child inherits it, so either value works) and selects the
    per-agent work queue.

    ``task_id`` is included in the event payload when this worker session
    was running for a subagent task (set by the ``spawn_task`` tool or
    the dispatcher tick).  The coordinator agent uses it to correlate
    the completion with the ``spawn_task`` call it made earlier; plain
    ``spawn_worker`` sessions pass ``None`` and the key is omitted.

    When ``task_id`` is set AND ``session_factory`` is provided, the
    function reads the Task row and overrides the auto-extracted result
    with ``task.result`` / ``task.result_metadata`` if the worker called
    the ``task_complete`` self-tool explicitly.  This keeps the
    parent's view of "what did the worker produce" consistent with what
    the worker chose to hand off (rather than the LLM's last response
    text, which may differ from the explicit summary).  When the worker
    completed naturally without ``task_complete``, ``task.result`` is
    typically ``None`` and we fall back to the extracted LLM response.
    """
    try:
        from surogates.harness.message_utils import extract_final_response

        events = await session_store.get_events(worker_session_id)
        final_response = extract_final_response(events)

        payload: dict[str, Any] = {
            "worker_id": str(worker_session_id),
            "result": final_response[:_MAX_RESULT_CHARS],
        }
        if task_id is not None:
            payload["task_id"] = str(task_id)
            # Override result/metadata with explicit handoff from
            # task_complete when the worker called it. Defensive: any
            # error reading the Task falls through to the LLM-response
            # default so we never lose the notification.
            if session_factory is not None:
                try:
                    from sqlalchemy import select as _sel
                    from surogates.db.models import Task as _Task

                    async with session_factory() as _db:
                        explicit = await _db.scalar(
                            _sel(_Task).where(_Task.id == task_id)
                        )
                        if explicit is not None and explicit.result is not None:
                            payload["result"] = (
                                explicit.result[:_MAX_RESULT_CHARS]
                            )
                        if explicit is not None and explicit.result_metadata is not None:
                            payload["metadata"] = explicit.result_metadata
                except Exception:
                    logger.warning(
                        "Failed to read task %s for completion override; "
                        "falling back to LLM-extracted result",
                        task_id, exc_info=True,
                    )

        await session_store.emit_event(
            parent_session_id,
            EventType.WORKER_COMPLETE,
            payload,
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
    task_id: UUID | None = None,
) -> None:
    """Emit a ``WORKER_FAILED`` event into the parent session and re-enqueue it.

    ``agent_id`` selects the per-agent work queue so the parent wakes on the
    same worker that owns its agent.

    ``task_id`` is included in the event payload when this worker session
    was running for a subagent task; ``None`` for plain spawn_worker.
    """
    try:
        payload: dict[str, Any] = {
            "worker_id": str(worker_session_id),
            "error": error[:2000],
        }
        if task_id is not None:
            payload["task_id"] = str(task_id)

        await session_store.emit_event(
            parent_session_id,
            EventType.WORKER_FAILED,
            payload,
        )

        if redis is not None:
            await enqueue_session(redis, agent_id, parent_session_id)

    except Exception:
        logger.exception(
            "Failed to notify parent %s of worker %s failure",
            parent_session_id,
            worker_session_id,
        )


