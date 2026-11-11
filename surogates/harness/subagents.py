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

#: The tools of the family, offered only with parity on.
PARITY_TOOLS: frozenset[str] = frozenset({"agent"})
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
    too.  Off: none of the family.
    """
    result = set(tools) - PARITY_TOOLS
    if parity_enabled(config) and REPLACED_TOOL in result:
        result.discard(REPLACED_TOOL)
        result.add("agent")
    return result


def dispatch_of(session: Any) -> dict | None:
    """How the ``agent`` tool started *session*; None for any other session."""
    value = (getattr(session, "config", None) or {}).get(DISPATCH_KEY)
    return value if isinstance(value, dict) else None


def runs_in_background(session: Any) -> bool:
    """Whether *session* is a sub-agent started in the background: its end is told to its parent as a notification."""
    dispatch = dispatch_of(session)
    return dispatch is not None and dispatch.get("mode") == "background" and getattr(session, "parent_id", None) is not None


async def tell_parent(
    *, store: Any, redis: Any, session: Any, status: str, result: str = "", error: str | None = None,
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
    if dispatch.get("tool_call_id"):
        payload["tool_call_id"] = dispatch["tool_call_id"]
    try:
        await notify_parent_of_task_event(
            session_store=store, parent_session_id=session.parent_id,
            event_type=EventType.AGENT_NOTIFICATION, payload=payload, redis=redis,
        )
    except Exception:
        logger.warning("Could not tell the parent of agent %s that it %s", session.id, status, exc_info=True)
