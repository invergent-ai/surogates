"""A project's thread tools: how its master gives out the project's work.

A thread is a worker session of the master that does one piece of the work
in a sandbox of its own and reports back at the end of every turn, as
``worker.complete``.  Only a project's master is sent these tools
(``PROJECT_THREAD_TOOLS``).
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any
from uuid import UUID

from surogates.config import INTERRUPT_CHANNEL_PREFIX, enqueue_session
from surogates.session.events import EventType
from surogates.tools.registry import ToolRegistry, ToolSchema
from surogates.workstreams import is_project_master

# The chat title's cap, since a thread's title is its chat's title.  Counted
# in UTF-16 units, as a project's name is and as the desktop shell counts.
_MAX_TITLE = 256
# The statuses the message route lets a message into; an archived thread was deleted.
_ACCEPTS_MESSAGES = ("active", "idle", "failed", "paused", "completed")

_START_THREAD_SCHEMA = ToolSchema(
    name="start_thread",
    description=(
        "Start a thread: a separate agent session that does one piece of the "
        "project's work in its own workspace and reports back to you when it "
        "has done it. Returns at once with the thread's id. Start several "
        "threads in one response for several unrelated tasks."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "A short name for the work, shown to the user on the thread's card.",
            },
            "goal": {
                "type": "string",
                "description": (
                    "What the thread must do, complete and self-contained. A thread "
                    "cannot see this conversation: give it the facts, the files to "
                    "use, and what done looks like."
                ),
            },
            "context": {
                "type": "string",
                "description": "Background the thread needs beyond the goal.",
            },
        },
        "required": ["title", "goal"],
        "additionalProperties": False,
    },
)


_THREAD_ID = {
    "type": "string",
    "description": "The thread's id, from start_thread or from its report.",
}

_MESSAGE_THREAD_SCHEMA = ToolSchema(
    name="message_thread",
    description=(
        "Send a follow-up to one of this project's threads: more work in its "
        "area, or a correction. A finished thread runs again with everything "
        "it already knows. Its next report comes back to you."
    ),
    parameters={
        "type": "object",
        "properties": {
            "thread_id": _THREAD_ID,
            "message": {"type": "string", "description": "The follow-up, complete in itself."},
        },
        "required": ["thread_id", "message"],
        "additionalProperties": False,
    },
)

_STOP_THREAD_SCHEMA = ToolSchema(
    name="stop_thread",
    description=(
        "Stop a working thread. It ends its turn without a report; a "
        "follow-up with message_thread starts it again."
    ),
    parameters={
        "type": "object",
        "properties": {
            "thread_id": _THREAD_ID,
            "reason": {"type": "string", "description": "Why the thread is stopped."},
        },
        "required": ["thread_id"],
        "additionalProperties": False,
    },
)


def register(registry: ToolRegistry) -> None:
    for name, schema, handler in (
        ("start_thread", _START_THREAD_SCHEMA, _start_thread_handler),
        ("message_thread", _MESSAGE_THREAD_SCHEMA, _message_thread_handler),
        ("stop_thread", _STOP_THREAD_SCHEMA, _stop_thread_handler),
    ):
        registry.register(name=name, schema=schema, handler=handler, toolset="core")


def _error(message: str) -> str:
    return json.dumps({"error": message})


def _text(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    return value.strip() if isinstance(value, str) else ""


async def _start_thread_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    from surogates.workstreams.threads import start_thread

    texts = {name: _text(arguments, name) for name in ("title", "goal", "context")}
    if not texts["title"]:
        return _error("title is required")
    # The title is the thread's session instructions and its report's header,
    # so a line break would let it write instructions of its own.
    if any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in texts["title"]):
        return _error("title must be one line")
    if len(texts["title"].encode("utf-16-le")) // 2 > _MAX_TITLE:
        return _error(f"title must be at most {_MAX_TITLE} characters")
    if not texts["goal"]:
        return _error("goal is required")

    session_store = kwargs["session_store"]
    master = await session_store.get_session(UUID(str(kwargs["session_id"])))
    if not is_project_master(master.config):
        return _error("Only a project's coordinator starts threads.")
    thread = await start_thread(
        session_store=session_store,
        session_factory=kwargs["session_factory"],
        redis=kwargs.get("redis"),
        master=master,
        live_config=kwargs.get("session_config"),
        **texts,
    )
    if thread is None:
        return _error("This project is archived.")
    return json.dumps({"thread_id": str(thread.id), "title": texts["title"], "status": "started"})


async def _own_thread(thread_id: str, **kwargs: Any) -> Any:
    """The thread *thread_id* of the calling master's project, or None.

    A thread belongs to the project its row names, so another project's
    thread, a plain child of the master, and any other session are refused.
    The caller must be the master: a thread carries its project's id too.
    """
    from surogates.workstreams.store import WorkstreamStore

    master_config = kwargs.get("session_config") or {}
    if not is_project_master(master_config):
        return None
    try:
        session_id = UUID(thread_id)
    except ValueError:
        return None
    row = await WorkstreamStore(kwargs["session_factory"]).get_thread(session_id)
    if row is None or str(row.workstream_id) != master_config.get("workstream_id"):
        return None
    return await kwargs["session_store"].get_session(session_id)


async def _message_thread_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    from surogates.workstreams.store import WorkstreamStore

    thread_id, message = _text(arguments, "thread_id"), _text(arguments, "message")
    if not message:
        return _error("message is required")
    thread = await _own_thread(thread_id, **kwargs)
    if thread is None:
        return _error(f"No thread {thread_id} in this project.")
    if thread.status not in _ACCEPTS_MESSAGES:
        return _error(f"Thread {thread_id} was deleted.")
    store = kwargs["session_store"]
    # As a message typed into the thread: a finished turn, a failed one or
    # a stopped one runs again, and its viewers see it resume.
    if thread.status in ("completed", "failed", "paused"):
        await store.update_session_status(thread.id, "active")
        await store.emit_event(thread.id, EventType.SESSION_RESUME, {})
    await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": message})
    await WorkstreamStore(kwargs["session_factory"]).reopen_thread(thread.id)
    redis = kwargs.get("redis")
    if redis is not None:
        await enqueue_session(
            redis, org_id=str(thread.org_id), agent_id=thread.agent_id, session_id=thread.id,
        )
    return json.dumps({"status": "sent", "thread_id": str(thread.id)})


async def _stop_thread_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    thread_id = _text(arguments, "thread_id")
    reason = _text(arguments, "reason") or "stopped by the coordinator"
    thread = await _own_thread(thread_id, **kwargs)
    if thread is None:
        return _error(f"No thread {thread_id} in this project.")
    if thread.status != "active":
        return json.dumps({"status": "not_running", "thread_id": str(thread.id)})
    # As the pause route stops a chat: the status first, so the thread
    # reads as stopped, then the interrupt that ends its turn.
    store = kwargs["session_store"]
    await store.update_session_status(thread.id, "paused")
    await store.emit_event(thread.id, EventType.SESSION_PAUSE, {"reason": reason})
    redis = kwargs.get("redis")
    if redis is not None:
        await redis.publish(f"{INTERRUPT_CHANNEL_PREFIX}:{thread.id}", json.dumps({"reason": reason}))
    return json.dumps({"status": "stopped", "thread_id": str(thread.id)})
