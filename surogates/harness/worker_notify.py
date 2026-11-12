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

from surogates.channels.constants import session_queue_priority
from surogates.config import enqueue_session
from surogates.session.events import EventType

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from surogates.session.store import SessionStore

logger = logging.getLogger(__name__)

# Maximum characters of the worker's final response to embed in the
# notification event.
_MAX_RESULT_CHARS: int = 10_000


#: The most excluded files and repositories a report's payload names.
_MAX_LEFT_OUT_NAMED = 200


def is_delegated(session: Any) -> bool:
    """Whether *session* is a helper the ``agent`` tool started (the delegation channel): its parent waits for it in the tool call that started
    it, and has its result as that call's result.  Its report says so (``delegated``), and the parent does not
    read it a second time (``loop_context_replay.worker_news``)."""
    return getattr(session, "channel", None) == "delegation"


async def _thread_title(session_factory: Any | None, worker_session_id: UUID) -> str | None:
    """The title of the project's thread *worker_session_id*; None for any other worker."""
    if session_factory is None:
        return None
    from surogates.workstreams.store import WorkstreamStore

    # A failed lookup reports the thread as a plain worker rather than
    # losing its report.
    try:
        row = await WorkstreamStore(session_factory).get_thread(worker_session_id)
    except Exception:
        logger.warning(
            "Failed to read thread %s; reporting it as a plain worker",
            worker_session_id, exc_info=True,
        )
        return None
    return row.title if row is not None else None


async def _parent_priority(session_store: SessionStore, parent_session_id: UUID) -> float:
    """The parent's place on the work queue: a case's parent stays behind interactive work.

    A parent whose row cannot be read is woken as ordinary work: its wake
    matters more than its place.
    """
    try:
        return session_queue_priority(await session_store.get_session(parent_session_id))
    except Exception:
        logger.warning(
            "Could not read parent session %s; waking it as ordinary work",
            parent_session_id, exc_info=True,
        )
        return 0.0


async def emit_and_wake(
    *,
    session_store: SessionStore,
    parent_session_id: UUID,
    event_type: EventType,
    payload: dict[str, Any],
    redis: Redis | None = None,
) -> None:
    """Emit *event_type* into the parent's log and re-enqueue the parent.

    For news written outside the parent's own wake (a background agent's
    end, a thread's redo): emitting alone is not enough, since a parent that
    is not already queued has nothing to re-read the event.

    The parent's own ``org_id``/``agent_id`` select the work queue, not the
    child's.  Failing to resolve the parent is logged and swallowed — the
    event write is the durable part, and the caller is mid-teardown.
    """"""
    try:
        await session_store.emit_event(parent_session_id, event_type, payload)
    except Exception:
        logger.exception(
            "Failed to emit %s on parent session %s",
            event_type.value, parent_session_id,
        )
        return

    if redis is None:
        return
    try:
        parent = await session_store.get_session(parent_session_id)
        await enqueue_session(
            redis,
            org_id=str(parent.org_id),
            agent_id=parent.agent_id,
            session_id=parent_session_id,
            priority=session_queue_priority(parent),
        )
    except Exception:
        logger.exception(
            "Emitted %s but failed to wake parent session %s; it will not "
            "see the signal until something else enqueues it",
            event_type.value, parent_session_id,
        )


async def notify_parent_on_completion(
    *,
    session_store: SessionStore,
    worker_session_id: UUID,
    parent_session_id: UUID,
    org_id: str,
    agent_id: str,
    redis: Redis | None = None,
    session_factory: Any | None = None,
    files: list[dict[str, Any]] | None = None,
    landing: dict[str, Any] | None = None,
    unkept: dict[str, Any] | None = None,
    set_aside: dict[str, Any] | None = None,
    delegated: bool = False,
) -> None:
    """Emit a ``WORKER_COMPLETE`` event into the parent session and re-enqueue it.

    Extracts the last ``LLM_RESPONSE`` content from the worker's event log
    and includes it (truncated) in the notification so the coordinator LLM
    can see what the worker produced.  ``agent_id`` is the parent's agent id
    (the child inherits it, so either value works) and selects the
    per-agent work queue.

    A project's thread also reports its ``title``, and the *files* its
    turn's summary named.  A turn that ended early, or whose summary was
    not written, has None, so its report lists no files rather than
    claiming none.  A thread whose turn landed reports its *landing*'s
    files instead, each landed or not merged, the excluded files it made,
    the folders inside a git repository it wrote into, the helpers' files
    its copy did not take up, and the landing's state when it did not
    complete.  A landing on the user's computer also says why it did not
    land, with its computer's word for a refusal, what its folder's helper
    found there, left by a landing cut short, and whether its thread was
    stopped once it was under way.  A thread on the user's computer reports
    what its computer set aside of its work since its last report
    (*set_aside*, ``local_landing.set_aside_of``).

    A thread's helper whose hand-back failed reports *unkept*: ``kept``
    false, and ``left``, the files kept apart, which its thread's copy lacks.

    A helper's report is marked *delegated* (``is_delegated``).
    """
    try:
        from surogates.harness.message_utils import extract_final_response

        events = await session_store.get_events(worker_session_id)
        final_response = extract_final_response(events)

        payload: dict[str, Any] = {
            "worker_id": str(worker_session_id),
            "result": final_response[:_MAX_RESULT_CHARS],
            **(unkept or {}),
        }
        if delegated:
            payload["delegated"] = True
        title = await _thread_title(session_factory, worker_session_id)
        if title is not None:
            payload["title"] = title
            if files is not None:
                payload["files"] = files
            for key in ("excluded", "repositories", "not_taken") if landing is not None else ():
                if landing.get(key):
                    # At most this many names, and how many there are in all.
                    payload[key] = landing[key][:_MAX_LEFT_OUT_NAMED]
                    if len(landing[key]) > _MAX_LEFT_OUT_NAMED:
                        payload[f"{key}_count"] = len(landing[key])
            if landing is not None and landing["state"] != "completed":
                payload["landing"] = landing["state"]
                if landing.get("saved"):
                    # Not landed, and not lost: the turn is on the thread's branch.
                    payload["saved"] = True
                for key in ("reason", "code"):
                    if landing.get(key):
                        # Why a landing on the user's computer did not land, and its computer's word for a refusal.
                        payload[f"landing_{key}"] = landing[key]
            if landing is not None and landing.get("stopped"):
                # Stopped once its landing had written a file: the landing was not interrupted.
                payload["landing_stopped"] = True
            if landing is not None and landing.get("recovery"):
                # What its folder's helper found, left there by a landing cut short: told whatever came of this one.
                payload["recovery"] = {key: found[:_MAX_LEFT_OUT_NAMED] for key, found in landing["recovery"].items()}
            if set_aside is not None:
                # What its computer set aside of its work, out of its copy there, since its last report: told once.
                payload["set_aside"] = set_aside

        await session_store.emit_event(
            parent_session_id,
            EventType.WORKER_COMPLETE,
            payload,
        )

        # Re-enqueue the parent so it wakes up.
        if redis is not None:
            await enqueue_session(
                redis,
                org_id=str(org_id),
                agent_id=agent_id,
                session_id=parent_session_id,
                priority=await _parent_priority(session_store, parent_session_id),
            )

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
    org_id: str,
    agent_id: str,
    error: str,
    redis: Redis | None = None,
    session_factory: Any | None = None,
    not_taken: list[str] | None = None,
    set_aside: dict[str, Any] | None = None,
    delegated: bool = False,
) -> None:
    """Emit a ``WORKER_FAILED`` event into the parent session and re-enqueue it.

    ``agent_id`` selects the per-agent work queue so the parent wakes on the
    same worker that owns its agent.

    A project's thread whose failed turn was kept also reports
    *not_taken*, the helpers' files its copy did not take up, as a landing
    reports them; and a thread on the user's computer what its computer set
    aside of its work since its last report, as a completed turn reports it.
    """
    try:
        payload: dict[str, Any] = {
            "worker_id": str(worker_session_id),
            "error": error[:2000],
        }
        if delegated:
            payload["delegated"] = True
        title = await _thread_title(session_factory, worker_session_id)
        if title is not None:
            payload["title"] = title
            if not_taken:
                # At most this many names, and how many there are in all.
                payload["not_taken"] = not_taken[:_MAX_LEFT_OUT_NAMED]
                if len(not_taken) > _MAX_LEFT_OUT_NAMED:
                    payload["not_taken_count"] = len(not_taken)
            if set_aside is not None:
                payload["set_aside"] = set_aside

        await session_store.emit_event(
            parent_session_id,
            EventType.WORKER_FAILED,
            payload,
        )

        if redis is not None:
            await enqueue_session(
                redis,
                org_id=str(org_id),
                agent_id=agent_id,
                session_id=parent_session_id,
                priority=await _parent_priority(session_store, parent_session_id),
            )

    except Exception:
        logger.exception(
            "Failed to notify parent %s of worker %s failure",
            parent_session_id,
            worker_session_id,
        )


