"""A project's thread tools: how its master gives out the project's work.

A thread is a worker session of the master that does one piece of the work
in a sandbox of its own and reports back at the end of every turn, as
``worker.complete``.  Only a project's master is sent these tools
(``PROJECT_THREAD_TOOLS``).
"""

from __future__ import annotations

import json
import unicodedata
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from surogates.config import INTERRUPT_CHANNEL_PREFIX, enqueue_session
from surogates.session.events import EventType
from surogates.tools.registry import ToolRegistry, ToolSchema
from surogates.workstreams import is_project_master
from surogates.workstreams.derive import GROUPS, derive_thread, question_of

# The chat title's cap, since a thread's title is its chat's title.  Counted
# in UTF-16 units, as a project's name is and as the desktop shell counts.
_MAX_TITLE = 256
# The statuses the message route lets a message into; an archived thread was deleted.
_ACCEPTS_MESSAGES = ("active", "idle", "failed", "paused", "completed")
# The first line of a follow-up, so the thread, and the user reading it, can
# tell the coordinator's words from the user's own.
_FROM_COORDINATOR = "[From the project's coordinator]"
# What a stopped thread's interrupt says.  Never the model's words: the
# dispatcher reads some reasons as commands (``_SESSION_GONE_REASONS``).
_STOP_REASON = "stopped by the coordinator"

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


_LIST_THREADS_SCHEMA = ToolSchema(
    name="list_threads",
    description=(
        "List this project's threads: each one's id and title, where it stands "
        "(waiting on the user, working, idle or resolved), why it waits, and its "
        "status line, which is the thread's own words. Call it to find a "
        "thread's id rather than guessing one."
    ),
    parameters={
        "type": "object",
        "properties": {
            "group": {"type": "string", "enum": list(GROUPS), "description": "Only the threads in this group."},
        },
        "additionalProperties": False,
    },
)

_READ_THREAD_SCHEMA = ToolSchema(
    name="read_thread",
    description=(
        "Read where one of this project's threads stands: its latest report as "
        "you received it, with its files, and the question it is waiting on the "
        "user to answer, if any. The user answers a thread's question in the "
        "thread; a message_thread does not answer it."
    ),
    parameters={
        "type": "object",
        "properties": {"thread_id": _THREAD_ID},
        "required": ["thread_id"],
        "additionalProperties": False,
    },
)


_RESOLVE_THREAD_SCHEMA = ToolSchema(
    name="resolve_thread",
    description=(
        "Resolve a thread whose work is done: it moves to Resolved in the "
        "project's overview. A thread still working is stopped first. A "
        "follow-up with message_thread reopens it."
    ),
    parameters={
        "type": "object",
        "properties": {"thread_id": _THREAD_ID},
        "required": ["thread_id"],
        "additionalProperties": False,
    },
)


_PROPOSE_THREADS_SCHEMA = ToolSchema(
    name="propose_threads",
    description=(
        "Propose threads for the user to start, instead of starting them: the "
        "user sees a card for each and starts the ones they want. Use it when "
        "the user wants to approve threads before they start, and for work on "
        "the user's computer (where: \"device\"), which only the user can "
        "start. Returns at once; you are told when the user starts one."
    ),
    parameters={
        "type": "object",
        "properties": {
            "threads": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": _START_THREAD_SCHEMA.parameters["properties"]["title"],
                        "goal": _START_THREAD_SCHEMA.parameters["properties"]["goal"],
                        "where": {
                            "type": "string",
                            "enum": ["cloud", "device"],
                            "description": "cloud for a thread of its own; device for work in a folder on the user's computer.",
                        },
                    },
                    "required": ["title", "goal", "where"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["threads"],
        "additionalProperties": False,
    },
)


def register(registry: ToolRegistry) -> None:
    for name, schema, handler in (
        ("start_thread", _START_THREAD_SCHEMA, _start_thread_handler),
        ("message_thread", _MESSAGE_THREAD_SCHEMA, _message_thread_handler),
        ("stop_thread", _STOP_THREAD_SCHEMA, _stop_thread_handler),
        ("list_threads", _LIST_THREADS_SCHEMA, _list_threads_handler),
        ("read_thread", _READ_THREAD_SCHEMA, _read_thread_handler),
        ("resolve_thread", _RESOLVE_THREAD_SCHEMA, _resolve_thread_handler),
        ("propose_threads", _PROPOSE_THREADS_SCHEMA, _propose_threads_handler),
    ):
        registry.register(name=name, schema=schema, handler=handler, toolset="core")


def _error(message: str) -> str:
    return json.dumps({"error": message})


def _text(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    return value.strip() if isinstance(value, str) else ""


def _malformed(texts: dict[str, str]) -> str | None:
    """What is wrong with a thread's title and goal, or None."""
    if not texts["title"]:
        return "title is required"
    # The title is the thread's session instructions and its report's header,
    # so a line break would let it write instructions of its own.
    if any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in texts["title"]):
        return "title must be one line"
    if len(texts["title"].encode("utf-16-le")) // 2 > _MAX_TITLE:
        return f"title must be at most {_MAX_TITLE} characters"
    if not texts["goal"]:
        return "goal is required"
    return None


async def _start_thread_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    from surogates.workstreams.threads import start_thread

    texts = {name: _text(arguments, name) for name in ("title", "goal", "context")}
    if (malformed := _malformed(texts)) is not None:
        return _error(malformed)

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
    # Out of Resolved first, so a follow-up that is written is also queued.
    await WorkstreamStore(kwargs["session_factory"]).reopen_thread(thread.id)
    # As a message typed into the thread: a finished turn, a failed one or
    # a stopped one runs again, and its viewers see it resume.
    if thread.status in ("completed", "failed", "paused"):
        await store.resume_session(thread.id)
    await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": f"{_FROM_COORDINATOR}\n{message}"})
    redis = kwargs.get("redis")
    if redis is not None:
        await enqueue_session(
            redis, org_id=str(thread.org_id), agent_id=thread.agent_id, session_id=thread.id,
        )
    return json.dumps({"status": "sent", "thread_id": str(thread.id)})


async def _stop(thread: Any, reason: str, **kwargs: Any) -> bool:
    """Stop *thread* as the pause route stops a chat; whether it was working.

    The status first, so the thread reads as stopped, then the interrupt
    that ends its turn.  A thread already paused is interrupted again: its
    turn may not have heard the first time.
    """
    from surogates.workstreams.store import WorkstreamStore

    stopped = await WorkstreamStore(kwargs["session_factory"]).pause_thread(thread.id)
    if stopped:
        await kwargs["session_store"].emit_event(thread.id, EventType.SESSION_PAUSE, {"reason": reason})
    redis = kwargs.get("redis")
    if redis is not None and (stopped or thread.status == "paused"):
        await redis.publish(f"{INTERRUPT_CHANNEL_PREFIX}:{thread.id}", json.dumps({"reason": _STOP_REASON}))
    return stopped


async def _stop_thread_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    thread_id = _text(arguments, "thread_id")
    thread = await _own_thread(thread_id, **kwargs)
    if thread is None:
        return _error(f"No thread {thread_id} in this project.")
    stopped = await _stop(thread, _text(arguments, "reason") or _STOP_REASON, **kwargs)
    return json.dumps({"status": "stopped" if stopped else "not_running", "thread_id": str(thread.id)})


async def _list_threads_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    from surogates.workstreams.store import WorkstreamStore

    group = arguments.get("group")
    if group is not None and group not in GROUPS:
        return _error(f"group must be one of: {', '.join(GROUPS)}")
    master_config = kwargs.get("session_config") or {}
    if not is_project_master(master_config):
        return _error("Only a project's coordinator lists its threads.")
    now = datetime.now(timezone.utc)
    rows = [
        derive_thread(facts, now=now)
        for facts in await WorkstreamStore(kwargs["session_factory"]).thread_facts(UUID(master_config["workstream_id"]))
    ]
    return json.dumps({"threads": [
        {"thread_id": row["id"], **{key: row[key] for key in ("title", "group", "reason", "status_line")}}
        for row in rows if group is None or row["group"] == group
    ]}, ensure_ascii=False)


async def _read_thread_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    from surogates.harness.loop_context_replay import worker_note
    from surogates.workstreams.store import WorkstreamStore

    thread_id = _text(arguments, "thread_id")
    thread = await _own_thread(thread_id, **kwargs)
    if thread is None:
        return _error(f"No thread {thread_id} in this project.")
    projects = WorkstreamStore(kwargs["session_factory"])
    master_config = kwargs["session_config"]
    found = await projects.thread_facts(UUID(master_config["workstream_id"]), thread_id=thread.id)
    if not found:
        return _error(f"Thread {thread_id} was deleted.")
    row = derive_thread(found[0], now=datetime.now(timezone.utc))
    report = await projects.latest_report(thread.parent_id, thread.id)
    asked = question_of(found[0])
    return json.dumps({
        "thread_id": row["id"],
        **{key: row[key] for key in ("title", "group", "reason", "status_line", "progress")},
        # The text the master was sent: the thread's words between the
        # harness's markers, and that turn's files.
        "report": worker_note(report.type, report.data)["content"] if report is not None else None,
        "question": asked.payload.get("questions") if asked is not None else None,
    }, ensure_ascii=False)


async def _resolve_thread_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    from surogates.workstreams.store import WorkstreamStore

    thread_id = _text(arguments, "thread_id")
    thread = await _own_thread(thread_id, **kwargs)
    if thread is None:
        return _error(f"No thread {thread_id} in this project.")
    if thread.status not in _ACCEPTS_MESSAGES:
        return _error(f"Thread {thread_id} was deleted.")
    # A resolved thread's work is done, so one still working stops first.
    await _stop(thread, "resolved by the coordinator", **kwargs)
    await WorkstreamStore(kwargs["session_factory"]).resolve_thread(thread.id)
    return json.dumps({"status": "resolved", "thread_id": str(thread.id)})


async def _propose_threads_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    proposed = arguments.get("threads")
    if not isinstance(proposed, list) or not proposed:
        return _error("threads is required")
    threads = []
    for key, thread in enumerate(proposed, 1):
        thread = thread if isinstance(thread, dict) else {}
        texts = {name: _text(thread, name) for name in ("title", "goal")}
        if (malformed := _malformed(texts)) is not None:
            return _error(f"threads[{key}]: {malformed}")
        if thread.get("where") not in ("cloud", "device"):
            return _error(f"threads[{key}]: where must be cloud or device")
        threads.append({"key": str(key), **texts, "where": thread["where"]})
    if not is_project_master(kwargs.get("session_config")):
        return _error("Only a project's coordinator proposes threads.")
    # The cards are drawn from this event, and a thread is started from it
    # by its proposal and key, so the request that starts it names nothing else.
    proposal_id = str(uuid4())
    await kwargs["session_store"].emit_event(
        UUID(str(kwargs["session_id"])), EventType.THREAD_PROPOSED,
        {"proposal_id": proposal_id, "threads": threads},
    )
    return json.dumps({
        "status": "proposed",
        "proposal_id": proposal_id,
        "threads": [{"key": t["key"], "title": t["title"]} for t in threads],
    }, ensure_ascii=False)
