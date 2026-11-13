"""Built-in ``agent`` tool: start a sub-agent, in the foreground or the background, fresh or forked.

The one way an agent starts a sub-agent (``surogates.harness.subagents``).
A sub-agent is a child session on the delegation channel, started through
``delegate._run_single_delegation``: tool inheritance, the depth limit, the
iteration cap and the ``delegation.*`` events.

- Background (the default for a main session): the call returns the
  ``agent_id`` at once.  When the agent ends, its parent gets an
  ``agent.notification`` with the report, read at its next request, or as the
  input of a new turn when it rests.  Over the background limit, a call that
  did not ask for the background waits instead.
- Foreground (``run_in_background=false``, and the default for any session
  with a parent, which reports its own end once): the call waits for the
  agent and returns its final report, with its ``agent_id``.
- ``fork``: the agent starts from a copy of this conversation up to the
  call, then the brief, instead of from the brief alone.

Nesting: an agent below the depth limit gets ``agent`` too; one at the limit
does not.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import weakref
from typing import Any
from uuid import UUID, uuid4

from surogates.harness.agent_news import fenced
from surogates.harness.subagents import (
    AGENT_BUDGET_KEY,
    DISPATCH_KEY,
    budget_for_children,
    budget_left,
    budget_of,
    dispatch_of,
    may_message,
    over_budget,
    resolve_agent,
)
from surogates.runtime.turn_slots import turn_waiting
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)

#: What a fork reads before its brief: the conversation above is its parent's.
FORK_NOTE = (
    "[You are a fork of the conversation above. The plan in it, and the work it lists, are your parent's: your "
    "parent carries on with them, and may have started other forks for other parts. Your part is only the task "
    "below. Do it yourself: start no agents of your own. Do not redo, finish or check anything else from the "
    "conversation. End with your report: it is all your parent will see of your work.]"
)

#: What a background agent's call returns, after its id.
STARTED_NOTE = (
    "It works in the background. Its report reaches you as a notification when it ends. Until then, "
    "do not do its task yourself, and never state or guess its result. Do not sleep or poll list_agents "
    "to wait for it: when you have nothing else to do, end your turn."
)

_AGENT_DESCRIPTION = (
    "Start a sub-agent: a separate session that works on your brief and gives back only its final report. "
    "A new agent sees nothing of this conversation, so its prompt must stand alone: the goal, what is known or "
    "ruled out, where to look, the scope, and what to report. With fork=true it starts from a copy of this "
    "conversation instead, and the prompt says which part is its own. "
    "In a main session the call returns the agent_id at once and the report reaches you as a notification; pass "
    "run_in_background=false when your next step needs the report. In a session that was itself started by "
    "another, the call waits for the report. Start several independent agents in one response to run them "
    "in parallel. To give an agent more work, a correction or a follow-up, use send_message with its agent_id or "
    "name: it keeps everything it has read."
)

_SEND_MESSAGE_DESCRIPTION = (
    "Send a message to another agent: one you started (its agent_id or name), the agent that started you (to=\"parent\"), "
    "or another agent your parent started. A running agent reads it before its next step. An agent you "
    "started that has finished resumes with everything it had and the message, and its new report reaches you "
    "as a notification. Use it to continue, correct or redirect an agent instead of starting a new one. The "
    "call does not wait for an answer: a reply comes later, as a message or a notification."
)

_AGENT_SCHEMA = ToolSchema(
    name="agent",
    description=_AGENT_DESCRIPTION,
    parameters={
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "A short name for the task (3-5 words), shown in its notification and in lists.",
            },
            "prompt": {
                "type": "string",
                "description": (
                    "The brief: what to do and what to report. Self-contained unless fork is true."
                ),
            },
            "agent_type": {
                "type": "string",
                "description": "Optional pre-configured sub-agent type (its prompt, tools, model and limits).",
            },
            "model": {
                "type": "string",
                "description": "Optional model for the agent; omitted, it runs on yours.",
            },
            "run_in_background": {
                "type": "boolean",
                "description": (
                    "true: return the agent_id at once and get its report as a notification. false: wait here for "
                    "the report. Omitted: a main session's agent runs in the background, any other session's waits."
                ),
            },
            "fork": {
                "type": "boolean",
                "description": (
                    "true: the agent starts from a copy of this conversation; say in the prompt which part is its "
                    "own. Default false: from the prompt alone."
                ),
            },
            "name": {
                "type": "string",
                "description": "Optional short name to address the agent by in send_message and stop_agent (letters, digits, - and _).",
            },
        },
        "required": ["description", "prompt"],
        "additionalProperties": False,
    },
)


_SEND_MESSAGE_SCHEMA = ToolSchema(
    name="send_message",
    description=_SEND_MESSAGE_DESCRIPTION,
    parameters={
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "The agent_id or name of the agent to message, or \"parent\" for the agent that started you.",
            },
            "message": {
                "type": "string",
                "description": "The message. Self-contained: what to do, and what to send back.",
            },
            "summary": {
                "type": "string",
                "description": "A short summary of the message (5-10 words), shown with it.",
            },
        },
        "required": ["to", "message", "summary"],
        "additionalProperties": False,
    },
)


_LIST_AGENTS_SCHEMA = ToolSchema(
    name="list_agents",
    description=(
        "List the agents you can reach: the ones you started (and theirs), the agent that started you, and the "
        "other agents it started, each with its agent_id, name (when it has one), task and status (running, completed, failed, stopped); "
        "and the background commands you started that still run. With agent_id, only that agent; a project's "
        "thread then comes with its latest report, its files and the question it waits on the user for."
    ),
    parameters={
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Optional: the agent_id or name of one agent to read."},
        },
        "additionalProperties": False,
    },
)

_STOP_AGENT_SCHEMA = ToolSchema(
    name="stop_agent",
    description=(
        "Stop an agent you started (with the agents it started), by its id or name, or a background command you "
        "started, by its id. "
        "A stopped agent keeps its context: send_message resumes it."
    ),
    parameters={
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "The agent_id or name of the agent, or the id of the background command."},
            "reason": {"type": "string", "description": "Why it is stopped (optional)."},
            "resolve": {
                "type": "boolean",
                "description": "In a project: move the thread to Resolved, stopping it first if it still works.",
            },
        },
        "required": ["agent_id"],
        "additionalProperties": False,
    },
)


_MONITOR_SCHEMA = ToolSchema(
    name="monitor",
    description=(
        "Watch a long-running command instead of polling it: the command runs in the background in your "
        "workspace, and each new line it prints reaches you as a notification (lines that come together "
        "arrive together, at most max_notifications_per_minute a minute). Use it to wait for an event: a "
        "log line (e.g. `tail -f app.log | grep --line-buffered ERROR`), a file appearing, a server "
        "answering (a loop that prints once the condition holds and exits). Print only what you want to "
        "hear about. It ends when the command exits, when timeout_seconds pass, or with stop_agent on its "
        "monitor_id. For a command that just runs to its end, use terminal."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to watch; each line it prints is a notification."},
            "description": {"type": "string", "description": "What it waits for (3-5 words), shown with each notification."},
            "timeout_seconds": {
                "type": "integer", "minimum": 10, "maximum": 3600,
                "description": "Stop watching (and kill the command) after this many seconds; default 600.",
            },
            "max_notifications_per_minute": {
                "type": "integer", "minimum": 1, "maximum": 30,
                "description": "The most notifications a minute; lines beyond wait for the next one. Default 6.",
            },
        },
        "required": ["command", "description"],
        "additionalProperties": False,
    },
)


def register(registry: ToolRegistry) -> None:
    """Register the ``agent`` tool family."""
    registry.register(name="agent", schema=_AGENT_SCHEMA, handler=_agent_handler, toolset="core")
    registry.register(name="send_message", schema=_SEND_MESSAGE_SCHEMA, handler=_send_message_handler, toolset="core")
    registry.register(name="list_agents", schema=_LIST_AGENTS_SCHEMA, handler=_list_agents_handler, toolset="core")
    registry.register(name="stop_agent", schema=_STOP_AGENT_SCHEMA, handler=_stop_agent_handler, toolset="core")
    registry.register(name="monitor", schema=_MONITOR_SCHEMA, handler=_monitor_handler, toolset="core")


#: One lock per session for its monitors' count and start; an entry lives while a call holds or waits for it.
_MONITOR_LOCKS: "weakref.WeakValueDictionary[UUID, asyncio.Lock]" = weakref.WeakValueDictionary()


def _lock_of(locks: Any, key: Any) -> asyncio.Lock:
    """The lock *locks* keeps for *key*: one while any call holds or waits for it (a weak map), never two."""
    lock = locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        locks[key] = lock
    return lock


def _bounded(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value) if value is not None else default
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


async def _monitor_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    """Start *command* where the session's commands run, with its lines kept, and hand it to the worker's watch."""
    from surogates.devices.binding import device_of
    from surogates.harness.background_watch import MAX_MONITORS_PER_SESSION, background_watch
    from surogates.harness.subagents import monitor_allowed, reports_elsewhere

    session_store = kwargs.get("session_store")
    me_id = kwargs.get("session_id")
    if session_store is None or me_id is None:
        return _error("monitor needs its session; none was given")
    command = str(arguments.get("command") or "").strip()
    if not command:
        return _error("command is required")
    description = " ".join(str(arguments.get("description") or "").split())[:80] or command[:60]
    timeout = _bounded(arguments.get("timeout_seconds"), 600, 10, 3600)
    cap = _bounded(arguments.get("max_notifications_per_minute"), 6, 1, 30)
    me = await session_store.get_session(UUID(str(me_id)))
    if device_of(me.config) is not None:
        return _error("monitor runs in a cloud sandbox only; on your computer, use terminal with background=true")
    if reports_elsewhere(me) or not monitor_allowed(me.config):
        # A sub-agent's work ends with its report, and a thread's sandbox with its turn: neither outlives a monitor.
        return _error("monitor is for the main session; here, use terminal and wait for the command")
    if not background_watch.configured:
        return _error("this worker cannot watch commands")
    # Counted and started under one lock per session: the calls of one response run at once.
    async with _lock_of(_MONITOR_LOCKS, me.id):
        if await background_watch.monitors_running(me.id) >= MAX_MONITORS_PER_SESSION:
            # Counted for all workers: monitors a turn on another worker started are the session's too.
            return _error(
                f"the monitor could not start: {MAX_MONITORS_PER_SESSION} monitors already run: stop one with "
                "stop_agent before you start another",
            )
        return await _start_monitor(me, command, description, timeout, cap, kwargs)


async def _start_monitor(me: Any, command: str, description: str, timeout: int, cap: int, kwargs: dict) -> str:
    """Start the checked monitor where the session's commands run, and hand it to the worker's watch."""
    from surogates.harness.background_watch import HERE, MAX_MONITORS_PER_SESSION, background_watch, monitor_task
    from surogates.sandbox.pool import SandboxPool, sandbox_session_key

    sandbox_pool = kwargs.get("sandbox_pool")
    if isinstance(sandbox_pool, SandboxPool):
        from surogates.harness.tool_exec import _build_session_sandbox_spec

        owner = sandbox_session_key(me)
        holds_copy = getattr(sandbox_pool, "holds_copy", None)
        if holds_copy is not None and holds_copy(owner):
            return _error("monitor is for the main session; here, use terminal and wait for the command")
        spec = await _build_session_sandbox_spec(me, kwargs.get("tenant"), owner, credential_vault=kwargs.get("credential_vault"))
        await sandbox_pool.ensure(owner, spec)
        try:
            # Counted again where the commands run: the sandbox's own registry.
            started = json.loads(await sandbox_pool.execute(owner, "_monitor", json.dumps({
                "command": command, "task_id": monitor_task(me.id), "max_running": MAX_MONITORS_PER_SESSION,
            })))
        except (TypeError, ValueError):
            started = {"error": "the sandbox gave no answer"}
    else:
        from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError
        from surogates.tools.workspace_io.local import LocalWorkspaceIO

        from surogates.tools.utils.process_registry import process_registry

        owner = HERE
        if process_registry.running_count(monitor_task(me.id)) >= MAX_MONITORS_PER_SESSION:
            started = {"error": f"{MAX_MONITORS_PER_SESSION} monitors already run"}
        else:
            try:
                started = await LocalWorkspaceIO(workspace_path=kwargs.get("workspace_path")).start(
                    command, workdir=None, task_id=monitor_task(me.id), pty=False,
                    notify_on_complete=True, watcher_interval=None, watch_lines=True,
                )
            except WorkspaceSandboxError as exc:
                started = {"error": str(exc)}
    proc_id = started.get("session_id") if isinstance(started, dict) else None
    if not proc_id:
        error = str((started or {}).get("error") or "it did not start")
        if "Unknown tool" in error:
            # The sandbox's image predates the monitor (#629): the deployment's image is older than its runtime.
            error = (
                "this sandbox's image is older than the monitor tool. A command that ends can run with terminal "
                "(background=true, notify_on_complete=true) instead, its end then reaching you as a notification "
                "where the image reports it; a command that never ends cannot be watched here"
            )
        elif "monitors already run" in error:
            error += ": stop one with stop_agent before you start another"
        return _error(f"the monitor could not start: {error}")
    await background_watch.watch(owner=owner, proc_id=proc_id, session_id=me.id, command=command,
                           monitor=(description, float(timeout), cap))
    return json.dumps({
        "monitor_id": proc_id, "status": "watching", "description": description, "timeout_seconds": timeout,
        "note": (
            f"Each new line it prints reaches you as a notification, at most {cap} a minute, until the command "
            "exits, its timeout passes, or you stop it with stop_agent."
        ),
    })


#: An agent's name: a letter, then letters, digits, - and _, 40 at most.
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}")
#: What a call with a name that cannot be one is answered.
_BAD_NAME = 'name must start with a letter and use only letters, digits, - and _ (at most 40); "parent" is taken'


def valid_name(name: str) -> bool:
    """Whether *name* can address an agent: never ``parent``, an id's shape, or a background command's."""
    if not _NAME.fullmatch(name) or name.lower() == "parent" or name.startswith("proc_"):
        return False
    try:
        UUID(name)
    except ValueError:
        return True
    return False


#: An agent's status, as the family names it, by its session's.
_STATUS = {"active": "running", "processing": "running", "completed": "completed", "failed": "failed", "paused": "stopped"}


async def _tree(kwargs: dict, root_id: UUID) -> list[dict]:
    """*root_id* and every session under it: id, parent, org, status and ``agent_dispatch``, in one read."""
    from sqlalchemy import text

    async with kwargs["session_factory"]() as db:
        rows = (await db.execute(text(
            """
            WITH RECURSIVE tree(id) AS (
                SELECT id FROM sessions WHERE id = :id
                UNION
                SELECT s.id FROM sessions s JOIN tree t ON s.parent_id = t.id
            )
            SELECT s.id, s.parent_id, s.org_id, s.status, s.channel, s.created_at, s.config -> 'agent_dispatch' AS dispatch
            FROM sessions s WHERE s.id IN (SELECT id FROM tree)
            """
        ), {"id": root_id})).mappings().all()
    return [dict(row) for row in rows]


def _family_below(rows: list[dict], top: UUID) -> list[dict]:
    """The sessions under *top* reached through agents of the family only: each hop an ``agent_dispatch``."""
    children: dict[UUID, list[dict]] = {}
    for row in rows:
        children.setdefault(row["parent_id"], []).append(row)
    out, stack = [], [top]
    while stack:
        for row in children.get(stack.pop(), []):
            if isinstance(row["dispatch"], dict):
                out.append(row)
                stack.append(row["id"])
    return out


async def reachable_agents(session_factory: Any, me: Any) -> list[tuple[dict, str]]:
    """The sessions *me* can reach, each with its relation: its own agents at any depth, through agents of the
    family only (``yours``); and, for an agent of the family, its parent (``parent``) and its siblings
    (``sibling``).  Only those of its organisation, none deleted."""
    kwargs = {"session_factory": session_factory}
    found: list[tuple[dict, str]] = [(row, "yours") for row in _family_below(await _tree(kwargs, me.id), me.id)]
    if me.parent_id is not None and dispatch_of(me) is not None:
        siblings = await _tree(kwargs, me.parent_id)
        parent = next((row for row in siblings if row["id"] == me.parent_id), None)
        if parent is not None:
            found.append((parent, "parent"))
        found += [
            (row, "sibling") for row in siblings
            if row["parent_id"] == me.parent_id and row["id"] != me.id and isinstance(row["dispatch"], dict)
        ]
    return [(row, relation) for row, relation in found if row["org_id"] == me.org_id and row["status"] != "archived"]


async def _list_agents_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    """The agents the caller can reach (``reachable_agents``), plus its background commands still running."""
    from surogates.harness.background_watch import background_watch

    session_store = kwargs.get("session_store")
    me_id = kwargs.get("session_id")
    if session_store is None or me_id is None or kwargs.get("session_factory") is None:
        return _error("list_agents needs its session; none was given")
    me = await session_store.get_session(UUID(str(me_id)))
    wanted = str(arguments.get("agent_id") or "").strip()
    from surogates.workstreams import is_project_master

    if is_project_master(me.config):
        return await _master_list(wanted, **kwargs)
    agents = []
    for row, relation in await reachable_agents(kwargs["session_factory"], me):
        entry = {"agent_id": str(row["id"]), "relation": relation, "status": _STATUS.get(row["status"], row["status"])}
        dispatch = row["dispatch"] if isinstance(row["dispatch"], dict) else None
        if dispatch is not None:
            if dispatch.get("name"):
                entry["name"] = dispatch["name"]
            entry["description"] = dispatch.get("description") or ""
            entry["mode"] = dispatch.get("mode")
            if relation == "yours" and row["parent_id"] != me.id:
                entry["started_by"] = str(row["parent_id"])
        agents.append(entry)
    if wanted:
        found = next((a for a in agents if wanted in (a["agent_id"], a.get("name"))), None)
        return json.dumps(found) if found is not None else _error(f"no agent {wanted} that you can reach")
    return json.dumps({"agents": agents, "background_commands": await background_watch.running_for(me.id)})


async def _master_list(wanted: str, **kwargs: Any) -> str:
    """A project master's ``list_agents``: its threads, or one thread's latest report."""
    from surogates.tools.builtin import threads

    if wanted:
        return json.dumps(_thread_answer(await threads._read_thread_handler({"thread_id": wanted}, **kwargs)),
                          ensure_ascii=False)
    listed = json.loads(await threads._list_threads_handler({}, **kwargs))
    if "error" in listed:
        return json.dumps(listed)
    return json.dumps({"agents": [_thread_answer(json.dumps(row)) for row in listed["threads"]]}, ensure_ascii=False)


#: What the interrupt of an agent's stop says: a fixed word, never the model's, which the dispatcher reads.
_STOP_SIGNAL = "stopped by agent"


async def _stop_agent_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    """Stop an agent below the caller, with the agents of the family below it, or a background command the
    caller started."""
    session_store = kwargs.get("session_store")
    me_id = kwargs.get("session_id")
    redis = kwargs.get("redis")
    session_factory = kwargs.get("session_factory")
    target = str(arguments.get("agent_id") or "").strip()
    if session_store is not None and me_id is not None and target.startswith("proc_"):
        return await _stop_process(target, **kwargs)
    if session_store is None or me_id is None or session_factory is None:
        return _error("stop_agent needs its session; none was given")
    reason = " ".join(str(arguments.get("reason") or "").split())[:200] or "stopped by the agent that started it"
    me = await session_store.get_session(UUID(str(me_id)))
    from surogates.workstreams import is_project_master

    if is_project_master(me.config):
        from surogates.tools.builtin import threads

        if arguments.get("resolve") is True:
            return json.dumps(_thread_answer(await threads._resolve_thread_handler({"thread_id": target}, **kwargs)))
        given = {"thread_id": target, **({"reason": arguments["reason"]} if arguments.get("reason") else {})}
        return json.dumps(_thread_answer(await threads._stop_thread_handler(given, **kwargs)))
    if arguments.get("resolve") is not None:
        return _error(_NOT_A_MASTER_RESOLVES)
    try:
        target_id = UUID(target)
    except ValueError:
        # A name: the newest of the caller's own agents with it, never a sibling's.
        found = await resolve_agent(session_store, session_factory, me, target, own_only=True)
        target_id = found.id if found is not None else None
    mine = {row["id"]: row for row in _family_below(await _tree(kwargs, me.id), me.id)}
    agent = mine.get(target_id)
    if agent is None or agent["org_id"] != me.org_id:
        return _error(f"no agent {target} that you started")
    if agent["status"] not in ("active", "processing"):
        return json.dumps({"status": _STATUS.get(agent["status"], agent["status"]), "agent_id": str(agent["id"]),
                           "note": "It is not running: there is nothing to stop."})
    stopped = await stop_tree(
        session_store=session_store, redis=redis, session_factory=session_factory, top_id=agent["id"], by=me.id,
        reason=reason, rows=list(mine.values()),
    )
    if not stopped:
        return json.dumps({"status": "completed", "agent_id": str(agent["id"]), "note": "It ended before the stop: there was nothing to stop."})
    return json.dumps({"status": "stopped", "agent_id": str(agent["id"]), "stopped": [str(sid) for sid in stopped],
                       "note": "A stopped agent keeps its context: send_message resumes it."
                       if agent["parent_id"] == me.id else "A stopped agent keeps its context."})


async def stop_tree(
    *, session_store: Any, redis: Any, session_factory: Any, top_id: UUID, by: UUID, reason: str,
    rows: list[dict] | None = None,
) -> list[UUID]:
    """Stop running agent *top_id* and the agents of the family below it, for *by*: the ids it stopped.

    *rows*, the session tree they are found in (``_tree``), is read from *top_id* down when not given.
    """
    from surogates.config import INTERRUPT_CHANNEL_PREFIX
    from surogates.devices.operations import DeviceOperations
    from surogates.harness.message_utils import extract_final_response
    from surogates.harness.subagents import runs_in_background, tell_parent

    kwargs = {"session_factory": session_factory}
    rows = rows if rows is not None else await _tree(kwargs, top_id)
    top = next((row for row in rows if row["id"] == top_id), None)
    stopped: list[UUID] = []
    for row in [top, *_family_below(rows, top_id)] if top is not None else []:
        # Paused only from running, the status and its event in one write: a turn that ended meanwhile stays
        # ended, and is told of once.
        if await session_store.pause_if_active(row["id"], {
            "reason": "stopped", "message": reason, "stopped_by": str(by),
        }):
            stopped.append(row["id"])
    if not stopped:
        return stopped
    try:
        # As the pause route does: what the stopped agents, and the helpers under them, left waiting on the
        # user's computer is cancelled.
        below = [row["id"] for row in await _tree(kwargs, top_id)]
        await DeviceOperations(session_factory, redis).cancel(below, sparing=below)
    except Exception:
        logger.warning("Could not cancel the device operations of the agents %s stopped", by, exc_info=True)
    for sid in stopped:
        if redis is not None:
            await redis.publish(f"{INTERRUPT_CHANNEL_PREFIX}:{sid}", json.dumps({"reason": _STOP_SIGNAL}))
        session = await session_store.get_session(sid)
        if runs_in_background(session):
            # The stop tells the agent that started it, once: the turn it interrupts does not.
            await tell_parent(
                store=session_store, redis=redis, session=session, status="killed",
                result=extract_final_response(await session_store.get_events(sid), fallback=""),
            )
    return stopped


async def _stop_process(proc_id: str, **kwargs: Any) -> str:
    """Kill background command *proc_id* where the caller's commands run: its sandbox, or this process."""
    from surogates.harness.background_watch import background_watch
    from surogates.sandbox.pool import SandboxPool

    session_store = kwargs["session_store"]
    me = await session_store.get_session(UUID(str(kwargs["session_id"])))
    if not background_watch.started_by(proc_id, me.id):
        # Watched by another worker's watch, which a turn of this session started: it kills it there.
        elsewhere = await background_watch.stop_elsewhere(proc_id, me.id)
        if elsewhere == "stopping":
            return json.dumps({"status": "stopping", "agent_id": proc_id,
                               "note": "It is being stopped. Its end reaches you as a notification."})
        if elsewhere == "unknown":
            return _error(f"background command {proc_id} could not be looked up just now: try again")
        if elsewhere == "gone":
            return _error(f"background command {proc_id} can no longer be stopped: it may have ended, or may still be running")
        return _error(f"no background command {proc_id} that you started and that still runs")
    sandbox_pool = kwargs.get("sandbox_pool")
    args = json.dumps({"action": "kill", "session_id": proc_id})
    owner = background_watch.owner_of(proc_id)
    if owner and isinstance(sandbox_pool, SandboxPool) and sandbox_pool.sandbox_of(owner):
        try:
            result = json.loads(await sandbox_pool.execute(owner, "process", args))
        except (TypeError, ValueError):
            result = {"status": "error"}
    else:
        from surogates.tools.utils.process_registry import process_registry

        result = process_registry.kill_process(proc_id)
    return json.dumps({"status": result.get("status", "stopped"), "agent_id": proc_id,
                       "note": "Its end reaches you as a notification."})


async def _send_message_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    """Deliver a message to an agent of the caller's tree; see ``send_message``'s description.

    It is written to the target as a ``coordinator.message``: a message
    typed into the session by another agent, never by its user.  A running
    target reads it at its next iteration (the harness's steering read), a
    resting one is resumed and queued, and a replay reads it where the live
    turn did.
    """
    from surogates.channels.constants import session_queue_priority
    from surogates.config import enqueue_session
    from surogates.session.events import EventType

    session_store = kwargs.get("session_store")
    sender_id = kwargs.get("session_id")
    if session_store is None or sender_id is None:
        return _error("send_message needs its session; none was given")
    to = str(arguments.get("to") or "").strip()
    message = str(arguments.get("message") or "").strip()
    summary = " ".join(str(arguments.get("summary") or "").split())[:120]
    if not to or not message:
        return _error("to and message are required")
    sender = await session_store.get_session(UUID(str(sender_id)))
    from surogates.workstreams import is_project_master

    if is_project_master(sender.config):
        # A project's master follows its threads up.
        from surogates.tools.builtin import threads

        answer = _thread_answer(await threads._message_thread_handler({"thread_id": to, "message": message}, **kwargs))
        if "error" in answer:
            return json.dumps(answer)
        return json.dumps({"status": "delivered", "to": answer["agent_id"],
                           "note": "The thread reads it, and its next report reaches you."})
    if to.lower() == "parent" and sender.parent_id is None:
        return _error("you were not started by another agent: there is no parent to message")
    from surogates.harness.subagents import parent_only

    # A sub-agent whose policy denies send_message has it from its gate for its parent alone.
    kept_only = getattr(kwargs.get("governance_gate"), "kept_only", None)
    parent_only_here = parent_only(sender.config, kept_only=bool(kept_only and kept_only("send_message")))
    if parent_only_here and to.lower() != "parent" and to != str(sender.parent_id):
        return _error("you may message only the agent that started you")
    try:
        target = await resolve_agent(session_store, kwargs.get("session_factory"), sender, to)
    except Exception:
        target = None
    if target is None or not await may_message(session_store, sender, target):
        # The same answer for an agent that does not exist and one that is not the caller's to reach.
        return _error(f"no agent {to} that you can message: only your parent, the agents you started, and the agents your parent started")
    if target.status == "archived":
        return _error(f"agent {target.id} was deleted")

    if target.id == sender.parent_id:
        head = f"[Message from agent {sender.id}"
        description = str((dispatch_of(sender) or {}).get("description") or "").replace("[", "(").replace("]", ")")
        head += f" ({description})" if description else ""
    elif sender.id == target.parent_id:
        head = "[Message from the agent that started you"
    else:
        head = f"[Message from agent {sender.id}"
    # The words are the sender's, fenced off as such: they cannot pass for the harness's own lines.
    summary = summary.replace("[", "(").replace("]", ")")
    content = f"{head}: {summary}]\n{fenced(message)}" if summary else f"{head}]\n{fenced(message)}"

    dispatch = dispatch_of(target)
    if dispatch is not None and dispatch.get("mode") != "background" and await _call_ended(session_store, target):
        # Its first report was its call's result; a report of a later run has no call to return to.
        await session_store.update_session_config_key(target.id, DISPATCH_KEY, {**dispatch, "mode": "background"})
    await session_store.emit_event(target.id, EventType.COORDINATOR_MESSAGE, {
        "content": content, "kind": "agent_message",
        "from_agent": str(sender.id), "to_agent": str(target.id), "summary": summary,
        # The sender's call: a message is joined to the turn that sent it.
        "tool_call_id": kwargs.get("tool_call_id"),
    })
    # Read after the message is in its log: a turn that ended meanwhile has not read it, and is resumed for it
    # as one at rest is, if the rules let the sender.  No other wake takes it up (``_has_stranded_user_message``).
    target = await session_store.get_session(target.id)
    running = target.status in ("active", "processing")
    resumed, why_not = False, None
    if not running and target.status != "archived":
        # Checked and resumed under the lock the sender's starts take: a resume and the ``agent`` calls of one
        # response run at once, and together they stay within the background cap.
        async with _start_lock(sender.id):
            why_not = await _why_not_resume(session_store, sender, target, kwargs.get("session_factory"))
            if why_not is None:
                resumed = await session_store.resume_if_resting(
                    target.id, (target.status,), {"source": "agent_message", "from_agent": str(sender.id)},
                )
        if why_not is None and not resumed:
            target = await session_store.get_session(target.id)
            running = target.status in ("active", "processing")
    elif not running:
        why_not = "it was deleted"
    redis = kwargs.get("redis")
    if redis is not None and (running or resumed):
        await enqueue_session(
            redis, org_id=str(target.org_id), agent_id=target.agent_id, session_id=target.id,
            priority=session_queue_priority(target),
        )
    if resumed:
        return json.dumps({"status": "resumed", "to": str(target.id), "note": "It was at rest: it resumes with the message."})
    if running:
        return json.dumps({"status": "delivered", "to": str(target.id), "note": "It reads the message before its next step."})
    return json.dumps({
        "status": "queued", "to": str(target.id),
        "note": (
            f"It rests and you may not resume it ({why_not}). Nothing wakes it for this message: do not wait "
            "for its reply."
        ),
    })


async def _call_ended(session_store: Any, target: Any) -> bool:
    """Whether the ``agent`` call that started foreground agent *target* has returned: its parent's log closes it."""
    from surogates.session.events import EventType

    ends = await session_store.get_events(
        target.parent_id, types=[EventType.DELEGATION_COMPLETE, EventType.DELEGATION_FAILED],
    )
    return any((e.data or {}).get("child_session_id") == str(target.id) for e in ends)


async def _why_not_resume(session_store: Any, sender: Any, target: Any, session_factory: Any) -> str | None:
    """Why *sender* may not resume resting *target*; None when it may.

    A sub-agent may wake the agent that started it from a finished turn,
    never from a stop or a failure: those are its user's.  An agent that
    started others itself (no helper, and no project's thread, whose helpers
    start from a hand-off) may resume its own finished or failed ones,
    within its background limit.  Nothing else is resumed: the message waits.
    """
    from surogates.workstreams import is_project_thread

    if budget_of(target) and await over_budget(session_store, target) is not None:
        # Resumed, it would end after one more call for its report, and its parent be told again.
        return "it used its token budget"
    if target.id == sender.parent_id:
        return None if target.status == "completed" else f"it is {target.status}: its user resumes it"
    if target.parent_id != sender.id:
        return "only the agent that started it resumes it"
    if getattr(sender, "channel", None) == "delegation" or is_project_thread(sender.config):
        return "a sub-agent does not resume agents: its parent does"
    if target.status == "paused":
        # Only a stop of its own: one by the user, or by another agent, is theirs to lift.
        from surogates.harness.subagents import stopped_by

        if await stopped_by(session_store, target.id) != str(sender.id):
            return "it was stopped by its user"
    elif target.status not in ("completed", "failed"):
        return f"it is {target.status}"
    starting = [mode for mode, *_ in _STARTING.get(sender.id, [])]
    running = await _running_agents(session_factory, sender.id) if session_factory is not None else []
    if sum(mode == "background" for mode in [r[0] for r in running] + starting) >= MAX_RUNNING_IN_BACKGROUND:
        return f"{MAX_RUNNING_IN_BACKGROUND} of your agents already run in the background"
    return None


def _error(message: str) -> str:
    return json.dumps({"error": message})


def _forget_start(parent_id: UUID, start: tuple[str, str | None, str, str]) -> None:
    """Count a start no more: its child is in the database, or it never came."""
    starting = _STARTING.get(parent_id)
    if starting and start in starting:
        starting.remove(start)
    if not starting:
        _STARTING.pop(parent_id, None)


async def _running_agents(
    session_factory: Any, parent_id: UUID,
) -> list[tuple[str | None, str | None, str | None, UUID]]:
    """The agents *parent_id* started that are running: each one's (mode, agent_type, brief, id), in one read."""
    from sqlalchemy import select

    from surogates.db.models import Session as SessionRow

    async with session_factory() as db:
        rows = (await db.execute(
            select(SessionRow.config, SessionRow.id).where(
                SessionRow.parent_id == parent_id, SessionRow.status.in_(("active", "processing")),
            ),
        )).all()
    return [
        (config[DISPATCH_KEY].get("mode"), config.get("agent_type"), config[DISPATCH_KEY].get("brief"), sid)
        for config, sid in rows if isinstance(config, dict) and isinstance(config.get(DISPATCH_KEY), dict)
    ]


def _brief_mark(prompt: str) -> str:
    """What two briefs that say the same thing share: a digest of their words."""
    return hashlib.sha256(" ".join(prompt.split()).encode()).hexdigest()[:16]


#: Starts under way in this process, by parent, until their child is in the database:
#: (mode, agent_type, mark, brief mark).
_STARTING: dict[UUID, list[tuple[str, str | None, str, str]]] = {}
#: One lock per parent for its starts and resumes; an entry lives while a call holds or waits for it (a weak map).
_START_LOCKS: "weakref.WeakValueDictionary[UUID, asyncio.Lock]" = weakref.WeakValueDictionary()
#: Tokens a sub-agent with a budget has handed to its agents still starting or running in this process, by its id.
_BUDGET_HANDED: dict[UUID, int] = {}


def _start_lock(parent_id: UUID) -> asyncio.Lock:
    """The one lock of *parent_id*'s starts and resumes: never two at once, as a map that drops idle locks
    could hand a second call a new one while a woken waiter still holds the old."""
    lock = _START_LOCKS.get(parent_id)
    if lock is None:
        lock = asyncio.Lock()
        _START_LOCKS[parent_id] = lock
    return lock


#: The most agents one session may have running in the background at once.
MAX_RUNNING_IN_BACKGROUND = 5


async def fork_seed(session_store: Any, parent: Any, tool_call_id: str | None) -> list[dict] | None:
    """The parent's conversation up to the response that made call *tool_call_id*, as replay rebuilds it.

    That response, and anything after it, is left out: a fork starts where
    its parent decided to start it, with no call of its parent's open.
    """
    from surogates.harness.loop_context_replay import replayed_messages
    from surogates.session.events import EventType

    events = await session_store.get_events(parent.id, exclude_types=[EventType.LLM_DELTA])
    messages = replayed_messages(events, (parent.config or {}).get("workspace_path"))
    for at in range(len(messages) - 1, -1, -1):
        calls = messages[at].get("tool_calls") or [] if messages[at].get("role") == "assistant" else []
        if any(call.get("id") == tool_call_id for call in calls):
            return messages[:at]
    # The call is in no response of the log: the copy would end on a call left open.
    return None


#: What a thread's start returns after its id.
_ITS_REPORT = "Its report reaches you when its turn ends; until then, do not do its work yourself."
THREAD_STARTED_NOTE = f"It works as a thread of this project. {_ITS_REPORT}"
#: What a session that is no project's master is told of a thread's argument, and what to do instead.
_NOT_A_MASTER_RESOLVES = "only a project's master resolves threads: leave out resolve to stop an agent"


def _thread_answer(raw: str) -> dict:
    """A thread handler's JSON answer with the family's names: ``agent_id`` for ``thread_id``, ``description``
    for ``title``."""
    answer = json.loads(raw)
    if "thread_id" in answer:
        answer["agent_id"] = answer.pop("thread_id")
    if "title" in answer:
        answer["description"] = answer.pop("title")
    return answer


def _in_the_familys_words(error: str) -> str:
    """A thread handler's refusal of a title or a goal, naming the ``agent`` arguments they came from."""
    return error.replace("title is required", "description is required").replace(
        "title must", "description must").replace("goal is required", "prompt is required")


async def _master_agent(arguments: dict[str, Any], **kwargs: Any) -> str:
    """A project master's ``agent``: start a thread, in the cloud or on the project's computer."""
    from surogates.tools.builtin import threads

    if arguments.get("fork") is True or arguments.get("agent_type") or arguments.get("name"):
        return _error("a thread starts from its brief alone: give its description and its prompt")
    # A call written as threads were once proposed starts nothing: the user may have asked to approve each one.
    if "propose" in arguments or "where" in arguments:
        return _error(
            "agent starts the thread at once and has no propose or where: to have the user approve it first, ask the "
            "user in this conversation, then call agent without them"
        )
    # As given: the thread handlers refuse what is not text.
    title, goal = arguments.get("description"), arguments.get("prompt")
    answer = _thread_answer(await threads._start_thread_handler({"title": title, "goal": goal}, **kwargs))
    if "error" in answer:
        return _error(_in_the_familys_words(answer["error"]))
    if answer.get("status") == "waiting":
        # A thread on the user's computer begins once that computer sets up its copy of the project's folder.
        computer = answer.pop("computer")
        note = (
            f"It starts once {computer} sets up its copy of the project's folder, as soon as that computer is "
            f"online; you hear when it starts, or why it could not. {_ITS_REPORT}"
        )
        return json.dumps({**answer, "note": note}, ensure_ascii=False)
    return json.dumps({**answer, "status": "running", "note": THREAD_STARTED_NOTE}, ensure_ascii=False)


async def _agent_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    """Start one sub-agent; see the module."""
    from surogates.tools.builtin import delegate

    session_store = kwargs.get("session_store")
    tenant = kwargs.get("tenant")
    parent_id = kwargs.get("session_id")
    budget = kwargs.get("budget")
    if session_store is None or tenant is None or parent_id is None:
        return _error("the agent tool needs its session; none was given")
    from surogates.workstreams import is_project_master

    parent = await session_store.get_session(UUID(str(parent_id)))
    if is_project_master(parent.config):
        # A project's master gives out threads.
        return await _master_agent(arguments, **kwargs)

    prompt = str(arguments.get("prompt") or "").strip()
    if not prompt:
        return _error("prompt is required: the brief the agent works on")
    description = " ".join(str(arguments.get("description") or "").split())[:80] or " ".join(prompt.split()[:5])
    asked = arguments.get("run_in_background")
    fork = arguments.get("fork") is True
    agent_type = str(arguments.get("agent_type") or "").strip() or None
    model = str(arguments.get("model") or "").strip() or None
    name = str(arguments.get("name") or "").strip() or None
    if name is not None and not valid_name(name):
        return _error(_BAD_NAME)

    parent_config = parent.config or {}
    # Omitted, an agent runs in the background for a main session; a session that reports its end elsewhere
    # (to its parent, to Keel, where its routine says) would hear of it only after that report: its agents wait.
    from surogates.harness.subagents import reports_elsewhere

    implicit = not isinstance(asked, bool)
    waits = reports_elsewhere(parent)
    background = asked is True or (implicit and not waits)
    depth = int(parent_config.get("delegation_depth") or 0)
    if depth >= delegate._MAX_DELEGATION_DEPTH:
        return _error(
            f"agent depth limit reached ({depth}/{delegate._MAX_DELEGATION_DEPTH}): do this task yourself.",
        )
    parent_type = parent_config.get("agent_type") or ""
    if parent_type in delegate._NON_RECURSIVE_AGENT_TYPES and agent_type == parent_type:
        return _error(f"agent_type {parent_type!r} cannot start another of its own type.")
    if background and getattr(parent, "channel", None) == "delegation":
        # A helper's parent hears of its end once: work it left running would end after that.
        return _error("a sub-agent cannot start agents in the background: start it in the foreground")
    if background and parent.parent_id is None and waits:
        # A case's step or work item reports to Keel, a routine's run where the routine says, as its turn ends.
        return _error(
            "this session reports its work when its turn ends, so it cannot start agents in the background: "
            "start it in the foreground"
        )
    # Its token budget: the deployment's, or its parent's key.  A sub-agent with a budget hands each agent it
    # starts at most half of what it has left, less what its agents still running were handed: the calls of
    # one response run at once, and it needs tokens of its own to read their reports.
    child_tokens = budget_for_children(parent_config)
    left = await budget_left(session_store, parent)
    handed = 0
    if left is not None:
        share = (left - _BUDGET_HANDED.get(parent.id, 0)) // 2
        if share <= 0:
            return _error("your token budget is used up: finish with what you have")
        child_tokens = handed = min(child_tokens, share) if child_tokens else share
        _BUDGET_HANDED[parent.id] = _BUDGET_HANDED.get(parent.id, 0) + handed
    try:
        return await _start_agent(
            kwargs, parent=parent, prompt=prompt, description=description, background=background,
            fork=fork, agent_type=agent_type, model=model, depth=depth, budget=budget, child_tokens=child_tokens,
            name=name, implicit=implicit,
        )
    finally:
        if handed:
            _BUDGET_HANDED[parent.id] -= handed
            if _BUDGET_HANDED[parent.id] <= 0:
                _BUDGET_HANDED.pop(parent.id, None)


async def _start_agent(
    kwargs: dict[str, Any], *, parent: Any, prompt: str, description: str,
    background: bool, fork: bool, agent_type: str | None, model: str | None, depth: int, budget: Any,
    child_tokens: int, name: str | None = None, implicit: bool = False,
) -> str:
    """Start the agent the checked call asks for, and wait for its report unless it runs in the background."""
    from surogates.tools.builtin import delegate

    session_store = kwargs.get("session_store")
    tenant = kwargs.get("tenant")
    child_depth = depth + 1
    # Below the depth limit an agent may start agents of its own.
    role = "orchestrator" if child_depth < delegate._MAX_DELEGATION_DEPTH else "leaf"
    cap = min(delegate._CHILD_MAX_ITERATIONS, budget.remaining if budget else delegate._CHILD_MAX_ITERATIONS)

    tool_call_id = kwargs.get("tool_call_id")
    seed = await fork_seed(session_store, parent, tool_call_id) if fork else None
    if fork and seed is None:
        return _error("this call cannot be forked from: start the agent with a brief instead")
    # Checked and counted under one lock per parent: the calls of one response run at once, and none of their
    # children is in the database yet.  The mode is settled here, before anything records it: a child stamped
    # for the background that ran in the foreground would report twice.
    async with _start_lock(parent.id):
        # The starts under way first, then the database: one inserted meanwhile is in one of the two.
        brief = _brief_mark(prompt)
        starting = _STARTING.get(parent.id, [])
        running = await _running_agents(kwargs.get("session_factory"), parent.id) if kwargs.get("session_factory") else []
        # A model that means to wait for a report can repeat its last call instead: the same agent again.
        same = next((str(sid) for _, _, mark, sid in running if mark == brief), None)
        if same is not None or any(mark == brief for *_, mark in starting):
            return _error(
                f"an agent with this brief is already running{f' ({same})' if same else ''}: wait for its "
                "notification instead of starting it again, and end your turn if you have nothing else to do",
            )
        under_way = [(mode, kind) for mode, kind, *_ in running] + [(mode, kind) for mode, kind, *_ in starting]
        if background and sum(mode == "background" for mode, _ in under_way) >= MAX_RUNNING_IN_BACKGROUND:
            if not implicit:
                return _error(
                    f"{MAX_RUNNING_IN_BACKGROUND} of your agents already run in the background: wait for their "
                    "reports, or stop one, before starting another",
                )
            # Not asked for the background: it waits here instead.
            background = False
        if agent_type in delegate._NON_RECURSIVE_AGENT_TYPES and any(kind == agent_type for _, kind in under_way):
            return _error(f"an agent of type {agent_type!r} already runs: one at a time")
        # This start, until its child is in the database: (mode, agent_type, a mark of its own).
        start = ("background" if background else "foreground", agent_type, uuid4().hex, brief)
        _STARTING.setdefault(parent.id, []).append(start)

    dispatch = {
        "description": description,
        "mode": "background" if background else "foreground",
        "fork": fork,
        "tool_call_id": tool_call_id,
        "parent": str(parent.id),
        "brief": brief,
        **({"name": name} if name else {}),
        # An agent a mission's coordinator starts is the mission's: its judge, budget and cascade read it.
        **({"mission_id": str(parent.config["active_mission_id"])}
           if (parent.config or {}).get("active_mission_id") else {}),
    }
    started: list[UUID] = []

    async def on_started(child: Any) -> str | None:
        started.append(child.id)
        # In the database now: counted there from here on.
        _forget_start(parent.id, start)
        if not background:
            return None
        return json.dumps({
            "agent_id": str(child.id), "status": "running", "description": description, "note": STARTED_NOTE,
        })

    run = delegate._run_single_delegation(
        task={
            "goal": f"{FORK_NOTE}\n\n{prompt}" if fork else prompt,
            "context": "", "model": model, "agent_type": agent_type, "role": role,
        },
        parent_session=parent,
        child_depth=child_depth,
        child_iteration_cap=cap,
        session_store=session_store,
        redis=kwargs.get("redis"),
        tenant=tenant,
        session_factory=kwargs.get("session_factory"),
        memory_manager=kwargs.get("memory_manager"),
        bundle=kwargs.get("bundle"),
        live_parent_config=kwargs.get("session_config"),
        leaf_tools=("agent",),
        extra_config={
            DISPATCH_KEY: dispatch,
            **({AGENT_BUDGET_KEY: {"tokens": child_tokens}} if child_tokens else {}),
        },
        seed=seed,
        start_extra={"agent": True, "description": description, "mode": dispatch["mode"], "fork": fork,
                     "tool_call_id": tool_call_id},
        on_started=on_started,
    )
    try:
        if background:
            return await run
        # The parent's turn gives its slots back while it waits.
        async with turn_waiting():
            result = await run
    finally:
        _forget_start(parent.id, start)
    if started and not result.startswith('{"error"'):
        result += f"\n\n[agent_id: {started[0]}]"
    return result
