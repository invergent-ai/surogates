"""Sub-agents an agent dispatches on its own: the ``agent`` tool family and what binds it together.

``agent`` starts a sub-agent in the foreground (it waits for the report) or in
the background (it returns the agent's id at once, and the agent's end reaches
its parent as an ``agent.notification``), from a fresh context or from a fork
of the conversation.  ``send_message``, ``list_agents``, ``stop_agent`` and
``monitor`` go with it.

A sub-agent the ``agent`` tool started carries ``agent_dispatch`` in its
config: what it was started for, how, and by which call.
"""

from __future__ import annotations

import logging
from typing import Any

from surogates.session.events import EventType

logger = logging.getLogger(__name__)

#: A sub-agent's config key: how the ``agent`` tool started it.
DISPATCH_KEY = "agent_dispatch"
#: A session's config key: the token budget of each agent it starts (``AgentsSettings.agent_token_budget``).
BUDGET_KEY = "agent_token_budget"
#: A sub-agent's config key: its own budget, ``{"tokens": N}``.
AGENT_BUDGET_KEY = "agent_budget"
#: The reason a turn ends with when its agent used its token budget: distinct from the iteration cap's
#: ``budget_exhausted``, which says nothing of tokens.
BUDGET_REASON = "token_budget_exhausted"
#: What an agent past its token budget is asked, in a last call with no tools: the report its parent receives.
TOKEN_BUDGET_REPORT_REQUEST = (
    "You have used the token budget you were given for this task, so you cannot call any more tools. Reply now "
    "with your report to the agent that started you: what you found and did so far, and what is still left undone."
)

#: The config keys of the family only the server writes: a client naming one could pose as another agent's
#: sub-agent, or lift its own depth or budget.
SERVER_OWNED_KEYS: tuple[str, ...] = (
    DISPATCH_KEY, "agent_token_budget", "agent_budget", "delegation_depth",
)

#: Policy names that stand for the family's tools: a policy that names ``terminal`` names ``monitor``, which
#: runs commands too.
STANDS_IN_FOR: dict[str, tuple[str, ...]] = {
    "terminal": ("monitor",),
}

#: The tools that go with ``agent``: offered to a session that may start agents, and to every agent it started.
COMPANION_TOOLS: frozenset[str] = frozenset({"send_message", "list_agents", "stop_agent"})
#: Offered with the companions where the session runs commands (``terminal``), never on the user's computer.
MONITOR_TOOL = "monitor"
#: The tools of the family.
FAMILY_TOOLS: frozenset[str] = frozenset({"agent", MONITOR_TOOL}) | COMPANION_TOOLS


def offer_family(tools: set[str], config: dict | None, *, root: bool = True) -> set[str]:
    """*tools*, a session's offered set, with the family's companions decided.

    ``agent`` stays where the session's filters left it.  Its companions
    (``COMPANION_TOOLS``) go to a session offered ``agent`` and to every agent
    the ``agent`` tool started, unless the session's config leaves them out by
    name; ``monitor`` only to a *root* session that runs commands
    (``monitor_allowed``).  What the session keeps whatever its tool list says
    (``kept_whatever_policy``) it has unless its config excludes it by name.
    """
    result = set(tools) - COMPANION_TOOLS - {MONITOR_TOOL}
    config = config or {}
    excluded = set(config.get("excluded_tools") or ())
    # A sub-agent can always reach the agent that started it, whatever its type's tool list, as a task worker
    # kept its self-tools; kept only this way, it reaches its parent and nobody else.  A project's master keeps
    # the family, its only way to give out work.
    kept = kept_whatever_policy(config, has_agents=False) - excluded
    if "agent" in result or isinstance(config.get(DISPATCH_KEY), dict) or kept:
        allowed = config.get("allowed_tools")
        from surogates.devices.binding import device_of

        companions = set(COMPANION_TOOLS)
        if "terminal" in result and device_of(config) is None and root and monitor_allowed(config):
            companions.add(MONITOR_TOOL)
        result |= {name for name in companions if name not in excluded and (not allowed or name in allowed)}
    return result | kept


def kept_whatever_policy(config: dict | None, *, has_agents: bool) -> frozenset[str]:
    """What of the family a session with *config* keeps whatever its agent's policy or its tool list says.

    A project's master keeps the family: Studio's policy editor does not
    know projects, and a master that can start no thread does no work.  A
    sub-agent keeps ``send_message``, to reach the agent that started it.  A
    session that started agents (*has_agents*) keeps listing, messaging and
    stopping them, so a policy cannot strand work that waits on it.  Nothing
    else: a policy that denies ``agent`` denies starting agents.
    """
    from surogates.workstreams import is_project_master

    config = config or {}
    kept: set[str] = set()
    if is_project_master(config):
        kept |= {"agent"} | COMPANION_TOOLS
    if isinstance(config.get(DISPATCH_KEY), dict):
        kept.add("send_message")
    if has_agents:
        kept |= COMPANION_TOOLS
    return frozenset(kept)


async def kept_family_gate(gate: Any, session: Any, store: Any) -> Any:
    """*gate*, an agent's policy, as *session* gets it: with what the session keeps of the family whatever the
    policy says (``kept_whatever_policy``).  The worker builds one per wake, and the harness, the prompt and
    every tool call read it."""
    from surogates.governance.policy import KeptFamilyGate

    has_agents = await store.has_event(session.id, EventType.DELEGATION_START)
    return KeptFamilyGate(gate, kept_whatever_policy(session.config, has_agents=has_agents))


#: The (agent type, tool) pairs this process has already warned of (``warn_unknown_tools``).
_WARNED_UNKNOWN_TOOLS: set[tuple[str, str]] = set()


def warn_unknown_tools(agents: Any, known: set[str]) -> None:
    """Warn, once per process for each agent type and name, of a tool an agent type's ``tools`` or
    ``disallowed_tools`` names that this runtime does not have: such a name is ignored.  A bundle published
    before a tool was removed still names it.  MCP tools are left out: they come and go with their servers."""
    for agent in agents:
        for name in [*(agent.tools or ()), *(agent.disallowed_tools or ())]:
            if name in known or name.startswith("mcp__") or (agent.name, name) in _WARNED_UNKNOWN_TOOLS:
                continue
            _WARNED_UNKNOWN_TOOLS.add((agent.name, name))
            logger.warning(
                "Agent type %r names the tool %r, which this runtime does not have; it is ignored", agent.name, name,
            )


def parent_only(config: dict | None, *, kept_only: bool = False) -> bool:
    """Whether a sub-agent with *config* has ``send_message`` only to reach its parent: its tool list leaves it
    out and ``offer_family`` put it back for that alone, or its agent's policy denies it and the session's gate
    keeps it for that alone (*kept_only*, ``KeptFamilyGate.kept_only``)."""
    config = config or {}
    if not isinstance(config.get(DISPATCH_KEY), dict):
        return False
    allowed = config.get("allowed_tools")
    return kept_only or (bool(allowed) and "send_message" not in allowed)


def reports_elsewhere(session: Any) -> bool:
    """Whether *session*'s end is reported somewhere other than to a person in it: a session with a parent to
    its parent, a Keel case's step or work item to Keel, a routine's run where the routine says.  Work it left
    running past its turn, an agent in the background or a monitor, would end after that report and wake a
    turn that is no step's, no item's, or announce a second time: such a session's agents run in the
    foreground, and it runs no monitor."""
    from surogates.keel.steps import is_case_work
    from surogates.session.execution_context import is_scheduled_run

    return getattr(session, "parent_id", None) is not None or is_case_work(session) or is_scheduled_run(session)


async def agents_run_in_its_sandbox(session_factory: Any, session: Any) -> bool:
    """Whether an agent below *session* still runs in the sandbox it shares with *session*.

    An agent works in its root's sandbox (``sandbox_session_key``), and a
    background one runs on past the turn that started it: the root's turn end
    lets that sandbox go only once none runs, or a command of the agent's dies
    with it.
    """
    if session_factory is None:
        return False
    from sqlalchemy import and_, exists, or_, select

    from surogates.db.models import Session as SessionRow
    from surogates.sandbox.pool import sandbox_session_key

    key = sandbox_session_key(session)
    stamped = SessionRow.config["sandbox_root_session_id"].astext
    async with session_factory() as db:
        # A child with no stamp works in its parent's (sandbox_session_key); a project's thread has its own.
        return bool(await db.scalar(select(exists().where(
            SessionRow.status == "active", SessionRow.id != session.id,
            or_(stamped == key, and_(stamped.is_(None), SessionRow.parent_id == session.id)),
        ))))


def monitor_allowed(config: dict | None) -> bool:
    """Whether a session with *config* may run monitors: not a sub-agent, whose work ends with its report,
    and not a project's thread or one under it, whose sandbox ends with its turn.  Either would be woken by
    lines after its end.  The callers also check the session itself (``reports_elsewhere``)."""
    from surogates.workstreams import under_a_thread

    config = config or {}
    return not isinstance(config.get(DISPATCH_KEY), dict) and not under_a_thread(config)


def budget_for_children(config: dict | None) -> int:
    """The token budget each agent a session with *config* starts gets: its own key, else the deployment's; 0 for none."""
    value = (config or {}).get(BUDGET_KEY)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    try:
        from surogates.config import load_settings

        return max(0, int(load_settings().agents.agent_token_budget))
    except Exception:
        return 0


def budget_of(session: Any) -> int:
    """The token budget of *session*, a sub-agent the ``agent`` tool started; 0 for none."""
    value = ((getattr(session, "config", None) or {}).get(AGENT_BUDGET_KEY) or {})
    tokens = value.get("tokens") if isinstance(value, dict) else None
    return tokens if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > 0 else 0


async def over_budget(store: Any, session: Any) -> int | None:
    """The tokens *session* and the agents under it have used, when that reaches its budget; None otherwise."""
    budget = budget_of(session)
    if not budget:
        return None
    used = await store.session_tree_tokens(session.id)
    return used if used >= budget else None


async def budget_left(store: Any, session: Any) -> int | None:
    """The tokens *session*, a sub-agent with a budget, may still use, its agents included; None for no budget."""
    budget = budget_of(session)
    if not budget:
        return None
    return max(budget - await store.session_tree_tokens(session.id), 0)


def dispatch_of(session: Any) -> dict | None:
    """How the ``agent`` tool started *session*; None for any other session."""
    value = (getattr(session, "config", None) or {}).get(DISPATCH_KEY)
    return value if isinstance(value, dict) else None


def runs_in_background(session: Any) -> bool:
    """Whether *session* is a sub-agent started in the background: its end is told to its parent as a notification."""
    dispatch = dispatch_of(session)
    return dispatch is not None and dispatch.get("mode") == "background" and getattr(session, "parent_id", None) is not None


#: The reasons a turn ends with that are no early stop: it finished.
_FINISHED = frozenset({"completed", "stop", "done", "complete"})
async def stopped_by(store: Any, session_id: Any) -> str | None:
    """The agent whose ``stop_agent`` paused the session last; None when its last stop was its user's.

    The pause the harness writes as the stopped turn ends (``turn_ended``) is
    not a stop of anyone's: the one before it is.
    """
    pauses = await store.get_events(session_id, types=[EventType.SESSION_PAUSE])
    for pause in reversed(pauses):
        data = pause.data or {}
        if data.get("turn_ended"):
            continue
        return data.get("stopped_by") or None
    return None


async def stopped_by_an_agent_this_run(store: Any, session_id: Any) -> bool:
    """Whether an agent's ``stop_agent`` stopped the session since its last resume (its run).  That stop told the
    agent that started it, once: a pause of its user's after it is the same stop, and no second notice."""
    events = await store.get_events(session_id, types=[EventType.SESSION_PAUSE, EventType.SESSION_RESUME])
    for event in reversed(events):
        if event.type == EventType.SESSION_RESUME.value:
            return False
        if (event.data or {}).get("stopped_by"):
            return True
    return False


def budget_report(events: list[Any]) -> str:
    """What an agent that ran out of tokens reports: the reply to its last tools-off call, or nothing; never a
    line from the middle of a step."""
    ends = [at for at, event in enumerate(events) if event.type == EventType.SESSION_COMPLETE.value]
    # The run ends at its last ``session.complete`` (events may follow it: captions, a message); it starts after
    # the end before that one.
    last = ends[-1] if ends else len(events)
    first = ends[-2] + 1 if len(ends) >= 2 else 0
    for event in reversed(events[first:last]):
        if event.type == EventType.LLM_RESPONSE.value and (event.data or {}).get("finish_reason") == BUDGET_REASON:
            content = ((event.data or {}).get("message") or {}).get("content")
            return content if isinstance(content, str) else ""
    return ""


def run_events(events: list[Any], status: str) -> list[Any]:
    """The events of the run of an agent that just ended with *status*.

    A ``completed`` or ``budget`` end has already written its run's
    ``session.complete``, so the run is after the one before it; a
    ``failed`` or ``killed`` end writes none, so the run is after the last
    one.  With no earlier end, the whole log.
    """
    def kind(event: Any) -> str:
        value = getattr(event, "type", None)
        return str(getattr(value, "value", value))

    ends = [at for at, event in enumerate(events) if kind(event) == EventType.SESSION_COMPLETE.value]
    if status in ("completed", "budget"):
        start = ends[-2] + 1 if len(ends) >= 2 else 0
    else:
        start = ends[-1] + 1 if ends else 0
    return events[start:]


async def _usage(store: Any, session: Any, status: str) -> dict[str, int]:
    """What agent *session* used: tokens over it and its own agents so far, its tool calls so far, and the
    duration of the run that just ended."""
    from datetime import datetime, timezone

    fresh = await store.get_session(session.id)
    events = await store.get_events(session.id, exclude_types=[EventType.LLM_DELTA])
    run = run_events(events, status)
    started = getattr(run[0], "created_at", None) if run else None
    started = started or getattr(fresh, "created_at", None)
    if started is not None and started.tzinfo is not None:
        started = started.astimezone(timezone.utc).replace(tzinfo=None)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return {
        "subagent_tokens": int(await store.session_tree_tokens(session.id)),
        "tool_uses": int(getattr(fresh, "tool_call_count", 0) or 0),
        "duration_ms": max(0, int((now - started).total_seconds() * 1000)) if started is not None else 0,
    }


async def resolve_agent(store: Any, session_factory: Any, caller: Any, to: str, *, own_only: bool = False) -> Any | None:
    """The session *to* names for *caller*: ``parent`` (any case), an id, or an agent's name.

    A name is looked for among the agents of the family *caller* can reach
    (its own at any depth, its parent and its siblings, as ``list_agents``
    shows them; with *own_only*, its own only), and the newest one with it
    answers.  Who may be messaged is ``may_message``'s to decide.
    """
    from uuid import UUID

    to = (to or "").strip()
    if to.lower() == "parent":
        return await store.get_session(caller.parent_id) if getattr(caller, "parent_id", None) else None
    try:
        session_id = UUID(to)
    except ValueError:
        session_id = None
    if session_id is not None:
        try:
            return await store.get_session(session_id)
        except Exception:
            return None
    if not to or session_factory is None:
        return None
    from surogates.tools.builtin.agents import reachable_agents

    rows = [
        row for row, relation in await reachable_agents(session_factory, caller)
        if (not own_only or relation == "yours") and (row["dispatch"] or {}).get("name") == to
    ]
    if not rows:
        return None
    newest = max(rows, key=lambda row: (row["created_at"], str(row["id"])))
    return await store.get_session(newest["id"])


async def lineage(store: Any, session: Any, *, limit: int = 8) -> list[Any]:
    """*session*'s ancestors, its parent first, at most *limit* of them."""
    chain: list[Any] = []
    current = session
    while getattr(current, "parent_id", None) is not None and len(chain) < limit:
        current = await store.get_session(current.parent_id)
        chain.append(current)
    return chain


async def may_message(store: Any, sender: Any, target: Any) -> bool:
    """Whether *sender* may send *target* a message: both of one organisation, and *target* the agent that
    started *sender*, or an agent the ``agent`` tool started within *sender*'s own tree (one of its own,
    at any depth, or one its parent started)."""
    if target.id == sender.id or target.org_id != sender.org_id:
        return False
    if target.id == sender.parent_id:
        # Only an agent the ``agent`` tool started speaks up to its parent: a thread, a routine's run or a
        # worker writes nothing into the session above it.
        return dispatch_of(sender) is not None
    if dispatch_of(target) is None:
        return False
    try:
        ancestors = await lineage(store, target)
    except Exception:
        return False
    if any(a.id == sender.id for a in ancestors):
        return True
    return sender.parent_id is not None and target.parent_id == sender.parent_id


async def tell_parent(
    *, store: Any, redis: Any, session: Any, status: str, result: str = "", error: str | None = None,
    reason: str | None = None,
) -> None:
    """Write the end of background sub-agent *session* to its parent as an ``agent.notification``, and wake it.

    *status* is ``completed``, ``failed``, ``killed`` or ``budget`` (it used
    its token budget); *result* the agent's last words.  Best effort: the agent's own log keeps its end whatever
    becomes of this.
    """
    from surogates.harness.agent_news import MAX_RESULT_CHARS, NEW_FORMAT
    from surogates.harness.worker_notify import emit_and_wake

    dispatch = dispatch_of(session) or {}
    payload: dict[str, Any] = {
        "agent_id": str(session.id), "kind": "agent", "status": status,
        "summary": str(dispatch.get("description") or ""), "result": (result or "")[:MAX_RESULT_CHARS],
        "format": NEW_FORMAT,
    }
    if status == "budget":
        payload["budget_tokens"] = budget_of(session)
    if error:
        payload["error"] = error[:2000]
    if reason and status == "completed" and reason not in _FINISHED:
        # Its turn ended early (a loop guard, a halted tool loop): said so, its words as they stand.
        payload["reason"] = reason
    if dispatch.get("tool_call_id"):
        payload["tool_call_id"] = dispatch["tool_call_id"]
    if dispatch.get("name"):
        payload["name"] = dispatch["name"]
    try:
        payload["usage"] = await _usage(store, session, status)
    except Exception:
        # Best effort: without its usage the notification still goes.
        logger.warning("Could not measure what agent %s used", session.id, exc_info=True)
    try:
        # The parent's log closes the agent's run as it closes a foreground one: what training and viewers join
        # on.  Before the notification, so the turn it gives reads past them; on their own, so a failure here
        # never keeps the notification from going.
        if status in ("completed", "budget"):
            from surogates.tools.builtin.delegate import _propagate_child_artifacts

            told = {
                (e.data or {}).get("artifact_id")
                for e in await store.get_events(session.parent_id, types=[EventType.ARTIFACT_CREATED])
                if (e.data or {}).get("originating_session_id") == str(session.id)
            }
            await _propagate_child_artifacts(
                session_store=store, parent_session_id=session.parent_id, child_session_id=session.id,
                # Each once: an agent that runs again reports again, its earlier artifacts already up.
                child_events=[
                    e for e in await store.get_events(session.id, types=[EventType.ARTIFACT_CREATED])
                    if (e.data or {}).get("artifact_id") not in told
                ],
            )
            await store.emit_event(session.parent_id, EventType.DELEGATION_COMPLETE, {
                "child_session_id": str(session.id), "goal": payload["summary"], "agent": True,
                "mode": "background", "tool_call_id": dispatch.get("tool_call_id"), "status": status,
            })
        elif status == "failed":
            await store.emit_event(session.parent_id, EventType.DELEGATION_FAILED, {
                "child_session_id": str(session.id), "goal": payload["summary"], "agent": True,
                "reason": f"Child session failed: {error or 'unknown error'}", "tool_call_id": dispatch.get("tool_call_id"),
            })
    except Exception:
        logger.warning("Could not close the run of agent %s in its parent's log", session.id, exc_info=True)
    try:
        await emit_and_wake(
            session_store=store, parent_session_id=session.parent_id,
            event_type=EventType.AGENT_NOTIFICATION, payload=payload, redis=redis,
        )
    except Exception:
        logger.warning("Could not tell the parent of agent %s that it %s", session.id, status, exc_info=True)
