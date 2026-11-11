"""Sub-agents an agent dispatches on its own: the ``agent`` tool family and what binds it together.

With ``subagent_parity`` on (``AgentsSettings``; a session's own config key
wins), a session is offered ``agent`` in place of ``delegate_task``: one tool
that starts a sub-agent in the foreground (it waits for the report) or in the
background (it returns the agent's id at once, and the agent's end reaches it
as an ``agent.notification``), from a fresh context or from a fork of the
conversation.  ``delegate_task`` stays callable under its old name.  Off, the
tools the model sees are those it always saw.

A sub-agent the ``agent`` tool started carries ``agent_dispatch`` in its
config: what it was started for, how, and by which call.
"""

from __future__ import annotations

import logging
from typing import Any

from surogates.session.events import EventType

logger = logging.getLogger(__name__)

#: A session's config key that turns the family on or off for it (and its sub-agents).
PARITY_KEY = "subagent_parity"
#: A sub-agent's config key: how the ``agent`` tool started it.
DISPATCH_KEY = "agent_dispatch"

#: The config keys of the family only the server writes: a client naming one could switch the family on
#: against the deployment's setting, pose as another agent's sub-agent, or lift its own depth or budget.
SERVER_OWNED_KEYS: tuple[str, ...] = (
    PARITY_KEY, DISPATCH_KEY, "agent_token_budget", "agent_budget", "delegation_depth",
)

#: Policy names that stand for the family's tools: a policy that names ``delegate_task`` names the
#: tools that take its place, and one that names ``terminal`` names ``monitor``.
STANDS_IN_FOR: dict[str, tuple[str, ...]] = {
    "delegate_task": ("agent", "send_message", "list_agents", "stop_agent"),
    "terminal": ("monitor",),
}

#: The tools that go with ``agent``: offered to a session that may start agents, and to every agent it started.
COMPANION_TOOLS: frozenset[str] = frozenset({"send_message", "list_agents", "stop_agent"})
#: The tools of the family, offered only with parity on.
PARITY_TOOLS: frozenset[str] = frozenset({"agent"}) | COMPANION_TOOLS
#: The tool ``agent`` stands in for; with parity on it is no longer offered, but still callable.
REPLACED_TOOL = "delegate_task"


def parity_enabled(config: dict | None) -> bool:
    """Whether a session with *config* has the ``agent`` tool family: its own key, else the deployment's setting."""
    value = (config or {}).get(PARITY_KEY)
    if isinstance(value, bool):
        return value
    try:
        from surogates.config import load_settings

        return bool(load_settings().agents.subagent_parity)
    except Exception:
        logger.debug("Could not read the agents settings; sub-agent parity stays off", exc_info=True)
        return False


def apply_parity(tools: set[str], config: dict | None) -> set[str]:
    """*tools*, a session's offered set, with the family in or out.

    On: ``agent`` takes ``delegate_task``'s place wherever the session may
    delegate, so a policy that denies ``delegate_task`` denies ``agent``
    too.  Its companions (``COMPANION_TOOLS``) go to such a session and to
    every agent the ``agent`` tool started, unless the session's config
    leaves them out by name.  Off: none of the family.
    """
    result = set(tools) - PARITY_TOOLS
    config = config or {}
    if not parity_enabled(config):
        return result
    if REPLACED_TOOL in result:
        result.discard(REPLACED_TOOL)
        if "agent" not in set(config.get("excluded_tools") or ()):
            result.add("agent")
    if "agent" in result or isinstance(config.get(DISPATCH_KEY), dict):
        allowed = config.get("allowed_tools")
        excluded = set(config.get("excluded_tools") or ())
        result |= {
            name for name in COMPANION_TOOLS
            if name not in excluded and (not allowed or name in allowed)
        }
    return result


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

    *status* is ``completed``, ``failed`` or ``killed``; *result* the agent's
    last words.  Best effort: the agent's own log keeps its end whatever
    becomes of this.
    """
    from surogates.harness.agent_news import MAX_RESULT_CHARS
    from surogates.harness.worker_notify import notify_parent_of_task_event

    dispatch = dispatch_of(session) or {}
    payload: dict[str, Any] = {
        "agent_id": str(session.id), "kind": "agent", "status": status,
        "summary": str(dispatch.get("description") or ""), "result": (result or "")[:MAX_RESULT_CHARS],
    }
    if error:
        payload["error"] = error[:2000]
    if reason and status == "completed" and reason not in _FINISHED:
        # Its turn ended early (a loop guard, a halted tool loop): said so, its words as they stand.
        payload["reason"] = reason
    if dispatch.get("tool_call_id"):
        payload["tool_call_id"] = dispatch["tool_call_id"]
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
        await notify_parent_of_task_event(
            session_store=store, parent_session_id=session.parent_id,
            event_type=EventType.AGENT_NOTIFICATION, payload=payload, redis=redis,
        )
    except Exception:
        logger.warning("Could not tell the parent of agent %s that it %s", session.id, status, exc_info=True)
