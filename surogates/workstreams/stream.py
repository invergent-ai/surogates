"""A project's stream: one Redis channel per project, naming each change to
its threads' rows, its counts and the cards in its master.

Nothing is written into the master's log for it.  Every event past the
master's cursor starts work, and one its replay does not show would start a
turn that sees nothing new.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from surogates.channels.memory_boundary import PROJECT_BOUNDARY_PREFIX
from surogates.session.events import EventType
from surogates.workstreams import THREAD

logger = logging.getLogger(__name__)

#: The events a project's stream names: what a thread's row, the project's
#: counts and a card in the master are derived from.  A streamed reply is
#: not one; its turn's end is.
STREAM_TYPES = frozenset(t.value for t in (
    EventType.USER_MESSAGE, EventType.HARNESS_WAKE,
    EventType.SESSION_RESUME, EventType.SESSION_COMPLETE, EventType.SESSION_FAIL, EventType.SESSION_PAUSE,
    EventType.INBOX_INPUT_REQUIRED, EventType.INBOX_ACTION_REQUIRED, EventType.INBOX_TASK_COMPLETE,
    EventType.INBOX_GOVERNANCE_GATE, EventType.INBOX_PROGRESS_CHECKIN, EventType.ASK_USER_QUESTION_RESPONSE,
    EventType.DEVICE_WAITING, EventType.DEVICE_RESUMED,
    EventType.TODO_UPDATED, EventType.ITERATION_SUMMARY, EventType.TURN_SUMMARY,
    EventType.WORKER_SPAWNED, EventType.WORKER_COMPLETE, EventType.WORKER_FAILED, EventType.THREAD_PROPOSED,
    EventType.HISTORY_REDO, EventType.COORDINATOR_MESSAGE,
))
#: What a project's stream names of its other sessions (the master, and the
#: sessions under a thread): only what changes a count or a card.  The rest of
#: their work changes no row.
PROJECT_WIDE_TYPES = frozenset(t.value for t in (
    EventType.INBOX_INPUT_REQUIRED, EventType.INBOX_ACTION_REQUIRED, EventType.INBOX_GOVERNANCE_GATE,
    EventType.ASK_USER_QUESTION_RESPONSE,
    EventType.WORKER_SPAWNED, EventType.WORKER_COMPLETE, EventType.WORKER_FAILED, EventType.THREAD_PROPOSED,
))
#: The changes named that are no event of a session's: the routes' and the
#: inbox sweeper's.
RESOLVED = "thread.resolved"
REOPENED = "thread.reopened"
EXPIRED = "inbox.expired"


def channel(workstream_id: UUID | str) -> str:
    return f"surogates:workstream:{workstream_id}"


def project_of(workspace_boundary: Any) -> str | None:
    """The project a session works in, by its workspace boundary: the
    master's, its threads' and every session's under them."""
    if isinstance(workspace_boundary, str) and workspace_boundary.startswith(PROJECT_BOUNDARY_PREFIX):
        return workspace_boundary[len(PROJECT_BOUNDARY_PREFIX):]
    return None


def heard(workspace_boundary: Any, workstream_role: Any, kind: str) -> str | None:
    """The project whose stream names a session's event of *kind*, or None:
    every change of a thread's, and of the project's other sessions only
    what changes a count or a card."""
    project = project_of(workspace_boundary)
    if project is None or kind not in (STREAM_TYPES if workstream_role == THREAD else PROJECT_WIDE_TYPES):
        return None
    return project


async def publish(redis: Any, workstream_id: UUID | str, session_id: UUID, kind: str) -> None:
    """Name a *kind* of change to *session_id* on the project's channel.

    Best effort, as the session channel is: a client refetches what it
    missed when its stream connects again.
    """
    if redis is None:
        return
    try:
        await redis.publish(channel(workstream_id), f"{session_id}:{kind}")
    except Exception:
        logger.debug("could not publish %s on project %s", kind, workstream_id, exc_info=True)


async def publish_session(redis: Any, session: Any, kind: str) -> None:
    """Name *kind* of change to *session* on its project's stream, when it
    works in a project: for a change a route makes after the event it wrote."""
    project = project_of((session.config or {}).get("workspace_boundary"))
    if project is not None:
        await publish(redis, project, session.id, kind)
