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

from surogates.harness.subagents import DISPATCH_KEY, PARITY_KEY
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
    "response to run them in parallel."
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


def register(registry: ToolRegistry) -> None:
    """Register the ``agent`` tool."""
    registry.register(name="agent", schema=_AGENT_SCHEMA, handler=_agent_handler, toolset="core")


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
