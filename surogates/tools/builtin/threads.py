"""How a project's master gives out the project's work: its threads.

A thread is a worker session of the master that does one piece of the work
in a sandbox of its own and reports back at the end of every turn, as
``worker.complete``.  The master reaches these through the ``agent`` family
(``tools.builtin.agents``): ``agent`` starts or proposes a thread,
``send_message`` follows one up, ``stop_agent`` stops or resolves one, and
``list_agents`` lists them or reads one.
"""

from __future__ import annotations

import json
import unicodedata
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from surogates.channels.constants import session_queue_priority
from surogates.config import enqueue_session
from surogates.devices.binding import device_of
from surogates.session.events import EventType
from surogates.workstreams import is_project_master
from surogates.workstreams import stream as project_stream
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
    device_id = device_of(master.config)
    if device_id is not None:
        return await _start_on_computer(master, device_id, texts, **kwargs)
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


async def _start_on_computer(master: Any, device_id: UUID, texts: dict[str, str], **kwargs: Any) -> str:
    """Make a thread of *master*'s project, which works on the user's computer *device_id*, and ask that computer to
    set up the thread's copy of the project's folder: the thread begins once it answers (``thread_bound``)."""
    import secrets

    from surogates.devices.operations import DeviceOperations
    from surogates.workstreams.store import WorkstreamStore
    from surogates.workstreams.threads import make_thread

    session_factory = kwargs["session_factory"]
    name = master.config["execution"].get("device_name")
    computer = name or "the user's computer"
    if await WorkstreamStore(session_factory).lost({master.id: device_id}):
        return _error(
            f"This project's folder can no longer be reached on {computer}: tell the user to open the project in "
            "Surogate Desktop on the computer it should work on and choose Use this computer for this project."
        )
    folder = master.config["workspace_path"]
    thread = await make_thread(
        session_store=kwargs["session_store"], session_factory=session_factory, master=master,
        live_config=kwargs.get("session_config"), title=texts["title"], device_id=device_id, device_name=name,
        folder=folder, start=texts["goal"],
    )
    if thread is None:
        return _error("This project is archived.")
    await DeviceOperations(session_factory, kwargs["redis"]).bind(
        session_id=thread.id, device_id=device_id, folder=folder, nonce=secrets.token_urlsafe(32),
        history=thread.id, master=master.id,
    )
    return json.dumps({"thread_id": str(thread.id), "title": texts["title"], "status": "waiting", "computer": computer})


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
    # The coordinator's own kind, never the user's message: it answers no wait on the user.
    await store.emit_event(thread.id, EventType.COORDINATOR_MESSAGE, {"content": f"{_FROM_COORDINATOR}\n{message}"})
    redis = kwargs.get("redis")
    if redis is not None:
        await enqueue_session(
            redis, org_id=str(thread.org_id), agent_id=thread.agent_id, session_id=thread.id,
            priority=session_queue_priority(thread),
        )
    return json.dumps({"status": "sent", "thread_id": str(thread.id)})


async def _stop(thread: Any, reason: str, **kwargs: Any) -> bool:
    """Stop *thread* for the coordinator; whether it was working."""
    from surogates.workstreams.threads import stop_thread

    return await stop_thread(
        thread, reason=reason, interrupt=_STOP_REASON, session_store=kwargs["session_store"], redis=kwargs.get("redis"),
    )


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
        for facts in await WorkstreamStore(kwargs["session_factory"]).thread_facts(
            UUID(master_config["workstream_id"]), with_files=False,
        )
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
    found = await projects.thread_facts(UUID(master_config["workstream_id"]), thread_id=thread.id, with_files=False)
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
    # After it is written, so whoever hears it reads the thread resolved.
    await project_stream.publish_session(kwargs.get("redis"), thread, project_stream.RESOLVED)
    return json.dumps({"status": "resolved", "thread_id": str(thread.id)})


async def _propose_threads_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    proposed = arguments.get("threads")
    if not isinstance(proposed, list) or not proposed:
        return _error("threads is required")
    # A project made on a computer has every thread on its folder there.
    on_computer = device_of(kwargs.get("session_config")) is not None
    threads = []
    for key, thread in enumerate(proposed, 1):
        thread = thread if isinstance(thread, dict) else {}
        texts = {name: _text(thread, name) for name in ("title", "goal")}
        if (malformed := _malformed(texts)) is not None:
            return _error(f"threads[{key}]: {malformed}")
        if thread.get("where") not in ("cloud", "device"):
            return _error(f"threads[{key}]: where must be cloud or device")
        if on_computer and thread["where"] != "device":
            return _error(
                f"threads[{key}]: this project works in a folder on the user's computer, "
                "and every thread works there: where must be device"
            )
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
