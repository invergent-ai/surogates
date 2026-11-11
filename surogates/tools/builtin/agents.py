"""Built-in ``agent`` tool: start a sub-agent, in the foreground or the background, fresh or forked.

Offered in place of ``delegate_task`` when sub-agent parity is on
(``surogates.harness.subagents``).  A sub-agent is a child session on the
delegation channel, started through ``delegate_task``'s own path
(``delegate._run_single_delegation``): the same tool inheritance, depth limit,
iteration cap and events.

- Foreground (the default): the call waits for the agent and returns its
  final report, with its ``agent_id``.
- ``run_in_background``: the call returns the ``agent_id`` at once.  When the
  agent ends, its parent gets an ``agent.notification`` with the report,
  read at its next request, or as the input of a new turn when it rests.
- ``fork``: the agent starts from a copy of this conversation up to the
  call, then the brief, instead of from the brief alone.

Nesting: an agent below the depth limit gets ``agent`` too; one at the limit
does not.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import UUID, uuid4

from surogates.harness.agent_news import fenced
from surogates.harness.subagents import DISPATCH_KEY, PARITY_KEY, dispatch_of, may_message
from surogates.runtime.turn_slots import turn_waiting
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)

#: What a fork reads before its brief: the conversation above is its parent's.
FORK_NOTE = (
    "[You are a fork of the conversation above, started to do one task. Do only the task below, "
    "then end with your report: it is all your parent will see of your work.]"
)

#: What a background agent's call returns, after its id.
STARTED_NOTE = (
    "It works in the background. Its report reaches you as a notification when it ends. Until then, "
    "do not do its task yourself, and never state or guess its result."
)

_AGENT_DESCRIPTION = (
    "Start a sub-agent: a separate session that works on your brief and gives back only its final report.\n"
    "Delegate independent pieces of work that can run in parallel, a side task that should not hold up "
    "your own work, or a question whose answer needs reading across many files or sources: you keep the "
    "conclusion, not the reading. Do it yourself when it is a handful of tool calls or you already know "
    "where to look. When in doubt, do not start one: each agent costs a whole session, and both hand-offs "
    "lose detail.\n"
    "A new agent sees nothing of this conversation. Its prompt must stand alone: the goal, what is already "
    "known or ruled out, where to look, the scope, and what to report. Its report can be wrong in the same "
    "confident tone as a right one; check what matters. With fork=true it starts from a copy of this "
    "conversation instead, and the prompt only says what to do.\n"
    "By default the call waits for the report. With run_in_background=true it returns the agent_id at once "
    "and you get a notification when the agent ends; work on meanwhile, never redo the work you handed it, "
    "and never state its result before the notification comes. Start several independent agents in one "
    "response to run them in parallel. To give an agent more work, a correction or a follow-up question, "
    "use send_message with its agent_id: it keeps everything it has read, which a new agent would not."
)

_SEND_MESSAGE_DESCRIPTION = (
    "Send a message to another agent: one you started (its agent_id), the agent that started you (to=\"parent\"), "
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
                "description": "true: return the agent_id at once and be notified when it ends. Default false: wait for its report.",
            },
            "fork": {
                "type": "boolean",
                "description": "true: the agent starts from a copy of this conversation. Default false: from the prompt alone.",
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
                "description": "The agent_id of the agent to message, or \"parent\" for the agent that started you.",
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
        "other agents it started, each with its agent_id, task and status (running, completed, failed, stopped); "
        "and the background commands you started that still run."
    ),
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
)

_STOP_AGENT_SCHEMA = ToolSchema(
    name="stop_agent",
    description=(
        "Stop an agent you started (with the agents it started), or a background command you started, by its id. "
        "A stopped agent keeps its context: send_message resumes it."
    ),
    parameters={
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "The agent_id of the agent, or the id of the background command."},
            "reason": {"type": "string", "description": "Why it is stopped (optional)."},
        },
        "required": ["agent_id"],
        "additionalProperties": False,
    },
)


def register(registry: ToolRegistry) -> None:
    """Register the ``agent`` tool family."""
    registry.register(name="agent", schema=_AGENT_SCHEMA, handler=_agent_handler, toolset="core")
    registry.register(name="send_message", schema=_SEND_MESSAGE_SCHEMA, handler=_send_message_handler, toolset="core")
    registry.register(name="list_agents", schema=_LIST_AGENTS_SCHEMA, handler=_list_agents_handler, toolset="core")
    registry.register(name="stop_agent", schema=_STOP_AGENT_SCHEMA, handler=_stop_agent_handler, toolset="core")


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
            SELECT s.id, s.parent_id, s.org_id, s.status, s.channel, s.config -> 'agent_dispatch' AS dispatch
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


async def _list_agents_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    """The agents the caller can reach: its own, at any depth, through agents of the family only; and, for an
    agent of the family, its parent and its siblings.  Plus its background commands still running."""
    from surogates.harness.background_watch import background_watch

    session_store = kwargs.get("session_store")
    me_id = kwargs.get("session_id")
    if session_store is None or me_id is None or kwargs.get("session_factory") is None:
        return _error("list_agents needs its session; none was given")
    me = await session_store.get_session(UUID(str(me_id)))
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
    agents = []
    for row, relation in found:
        if row["org_id"] != me.org_id or row["status"] == "archived":
            continue
        entry = {"agent_id": str(row["id"]), "relation": relation, "status": _STATUS.get(row["status"], row["status"])}
        dispatch = row["dispatch"] if isinstance(row["dispatch"], dict) else None
        if dispatch is not None:
            entry["description"] = dispatch.get("description") or ""
            entry["mode"] = dispatch.get("mode")
            if relation == "yours" and row["parent_id"] != me.id:
                entry["started_by"] = str(row["parent_id"])
        agents.append(entry)
    return json.dumps({"agents": agents, "background_commands": background_watch.running_for(me.id)})


#: What the interrupt of an agent's stop says: a fixed word, never the model's, which the dispatcher reads.
_STOP_SIGNAL = "stopped by agent"


async def _stop_agent_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    """Stop an agent below the caller, with the agents of the family below it, or a background command the
    caller started."""
    from surogates.config import INTERRUPT_CHANNEL_PREFIX
    from surogates.devices.operations import DeviceOperations
    from surogates.harness.message_utils import extract_final_response
    from surogates.harness.subagents import runs_in_background, tell_parent
    from surogates.session.events import EventType

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
    try:
        target_id = UUID(target)
    except ValueError:
        target_id = None
    mine = {row["id"]: row for row in _family_below(await _tree(kwargs, me.id), me.id)}
    agent = mine.get(target_id)
    if agent is None or agent["org_id"] != me.org_id:
        return _error(f"no agent {target} that you started")
    if agent["status"] not in ("active", "processing"):
        return json.dumps({"status": _STATUS.get(agent["status"], agent["status"]), "agent_id": str(agent["id"]),
                           "note": "It is not running: there is nothing to stop."})
    stopped: list[UUID] = []
    for row in [agent, *_family_below(list(mine.values()), agent["id"])]:
        # Paused only from running, the status and its event in one write: a turn that ended meanwhile stays
        # ended, and is told of once.
        if await session_store.pause_if_active(row["id"], {
            "reason": "stopped", "message": reason, "stopped_by": str(me.id),
        }):
            stopped.append(row["id"])
    if not stopped:
        return json.dumps({"status": "completed", "agent_id": target, "note": "It ended before the stop: there was nothing to stop."})
    try:
        # As the pause route does: what the stopped agents, and the helpers under them, left waiting on the
        # user's computer is cancelled.
        below = [row["id"] for row in await _tree(kwargs, agent["id"])]
        await DeviceOperations(session_factory, redis).cancel(below, sparing=below)
    except Exception:
        logger.warning("Could not cancel the device operations of the agents %s stopped", me.id, exc_info=True)
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
    return json.dumps({"status": "stopped", "agent_id": target, "stopped": [str(sid) for sid in stopped],
                       "note": "A stopped agent keeps its context: send_message resumes it."
                       if agent["parent_id"] == me.id else "A stopped agent keeps its context."})


async def _stop_process(proc_id: str, **kwargs: Any) -> str:
    """Kill background command *proc_id* where the caller's commands run: its sandbox, or this process."""
    from surogates.harness.background_watch import background_watch
    from surogates.sandbox.pool import SandboxPool

    session_store = kwargs["session_store"]
    me = await session_store.get_session(UUID(str(kwargs["session_id"])))
    if not background_watch.started_by(proc_id, me.id):
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
    if to.lower() == "parent":
        if sender.parent_id is None:
            return _error("you were not started by another agent: there is no parent to message")
        target_id = sender.parent_id
    else:
        try:
            target_id = UUID(to)
        except ValueError:
            return _error(f"{to!r} is not an agent_id")
    try:
        target = await session_store.get_session(target_id)
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
    })
    # Read after the message is in its log: a turn that ended meanwhile has not read it, and is resumed for it
    # as one at rest is, if the rules let the sender.  No other wake takes it up (``_has_stranded_user_message``).
    target = await session_store.get_session(target.id)
    running = target.status in ("active", "processing")
    resumed, why_not = False, None
    if not running and target.status != "archived":
        why_not = await _why_not_resume(session_store, sender, target, kwargs.get("session_factory"))
        if why_not is None:
            resumed = await session_store.resume_if_resting(
                target.id, (target.status,), {"source": "agent_message", "from_agent": str(sender.id)},
            )
            if not resumed:
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
    if session_factory is not None and sum(
        mode == "background" for mode, _ in await _running_agents(session_factory, sender.id)
    ) >= MAX_RUNNING_IN_BACKGROUND:
        return f"{MAX_RUNNING_IN_BACKGROUND} of your agents already run in the background"
    return None


def _error(message: str) -> str:
    return json.dumps({"error": message})


def _forget_start(parent_id: UUID, start: tuple[str, str | None, str]) -> None:
    """Count a start no more: its child is in the database, or it never came."""
    starting = _STARTING.get(parent_id)
    if starting and start in starting:
        starting.remove(start)
    if not starting:
        _STARTING.pop(parent_id, None)
        lock = _START_LOCKS.get(parent_id)
        if lock is not None and not lock.locked():
            _START_LOCKS.pop(parent_id, None)


async def _running_agents(session_factory: Any, parent_id: UUID) -> list[tuple[str | None, str | None]]:
    """The agents *parent_id* started that are running: each one's (mode, agent_type), in one read."""
    from sqlalchemy import select

    from surogates.db.models import Session as SessionRow

    async with session_factory() as db:
        rows = (await db.execute(
            select(SessionRow.config).where(
                SessionRow.parent_id == parent_id, SessionRow.status.in_(("active", "processing")),
            ),
        )).scalars().all()
    return [
        ((config.get(DISPATCH_KEY) or {}).get("mode"), config.get("agent_type"))
        for config in rows if isinstance(config, dict) and isinstance(config.get(DISPATCH_KEY), dict)
    ]


#: Starts under way in this process, by parent, until their child is in the database: (mode, agent_type, mark).
_STARTING: dict[UUID, list[tuple[str, str | None, str]]] = {}
_START_LOCKS: dict[UUID, asyncio.Lock] = {}


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


async def _agent_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    """Start one sub-agent; see the module."""
    from surogates.tools.builtin import delegate

    session_store = kwargs.get("session_store")
    tenant = kwargs.get("tenant")
    parent_id = kwargs.get("session_id")
    budget = kwargs.get("budget")
    if session_store is None or tenant is None or parent_id is None:
        return _error("the agent tool needs its session; none was given")

    prompt = str(arguments.get("prompt") or "").strip()
    if not prompt:
        return _error("prompt is required: the brief the agent works on")
    description = " ".join(str(arguments.get("description") or "").split())[:80] or " ".join(prompt.split()[:5])
    background = arguments.get("run_in_background") is True
    fork = arguments.get("fork") is True
    agent_type = str(arguments.get("agent_type") or "").strip() or None
    model = str(arguments.get("model") or "").strip() or None

    parent = await session_store.get_session(UUID(str(parent_id)))
    parent_config = parent.config or {}
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
    child_depth = depth + 1
    # Below the depth limit an agent may start agents of its own.
    role = "orchestrator" if child_depth < delegate._MAX_DELEGATION_DEPTH else "leaf"
    cap = min(delegate._CHILD_MAX_ITERATIONS, budget.remaining if budget else delegate._CHILD_MAX_ITERATIONS)

    tool_call_id = kwargs.get("tool_call_id")
    seed = await fork_seed(session_store, parent, tool_call_id) if fork else None
    if fork and seed is None:
        return _error("this call cannot be forked from: start the agent with a brief instead")
    dispatch = {
        "description": description,
        "mode": "background" if background else "foreground",
        "fork": fork,
        "tool_call_id": tool_call_id,
        "parent": str(parent.id),
    }
    started: list[UUID] = []
    # This start, until its child is in the database: (mode, agent_type, a mark of its own).
    start = ("background" if background else "foreground", agent_type, uuid4().hex)

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
        leaf_tools=("delegate_task", "agent"),
        extra_config={PARITY_KEY: True, DISPATCH_KEY: dispatch},
        seed=seed,
        start_extra={"agent": True, "description": description, "mode": dispatch["mode"], "fork": fork,
                     "tool_call_id": tool_call_id},
        on_started=on_started,
    )
    # Checked and counted under one lock per parent: the calls of one response
    # run at once, and none of their children is in the database yet.
    lock = _START_LOCKS.setdefault(parent.id, asyncio.Lock())
    async with lock:
        running = await _running_agents(kwargs.get("session_factory"), parent.id) if kwargs.get("session_factory") else []
        under_way = running + [(mode, kind) for mode, kind, _ in _STARTING.get(parent.id, [])]
        if background and sum(mode == "background" for mode, _ in under_way) >= MAX_RUNNING_IN_BACKGROUND:
            run.close()
            return _error(
                f"{MAX_RUNNING_IN_BACKGROUND} of your agents already run in the background: wait for their "
                "reports, or stop one, before starting another",
            )
        if agent_type in delegate._NON_RECURSIVE_AGENT_TYPES and any(kind == agent_type for _, kind in under_way):
            run.close()
            return _error(f"an agent of type {agent_type!r} already runs: one at a time")
        _STARTING.setdefault(parent.id, []).append(start)
    try:
        if background:
            return await run
        # The parent's turn gives its slots back while it waits, as delegate_task's does.
        async with turn_waiting():
            result = await run
    finally:
        _forget_start(parent.id, start)
    if started and not result.startswith('{"error"'):
        result += f"\n\n[agent_id: {started[0]}]"
    return result
