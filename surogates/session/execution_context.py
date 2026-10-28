"""What a session hands to the sessions made for it.

A routine's run and a helper (delegation, worker, task) act for the same
person, in the same project, with the same browser login, as the
conversation that made them.  These fields are copied from the parent
(or, for a routine, from what the schedule captured when it was made) and
never from the config the caller builds, which a tool's arguments can
reach.
"""

from __future__ import annotations

from typing import Any

from surogates.workstreams import is_project_thread

#: Sessions with nobody behind them to answer a question.
UNATTENDED_CHANNELS: frozenset[str] = frozenset({"scheduled", "delegation", "worker", "task"})

#: ``ops``: the Studio operator and project the work is for (MCP ``_meta``,
#: owner-scoped reads).  ``operator``: the name the prompt gives the person.
#: ``browser``: the login profile.  ``origin_channel``: where the work began.
#: Every kind of child inherits all four.
CONTEXT_FIELDS: tuple[str, ...] = ("ops", "operator", "browser", "origin_channel")

#: A schedule also keeps which sub-agent made it: its runs act as that one,
#: with what it is offered and denied (``user_reports``, owner scope).  A
#: helper is not given its parent's: it names its own, or is the agent itself.
SCHEDULE_FIELDS: tuple[str, ...] = (*CONTEXT_FIELDS, "agent_type")


def is_unattended(session: Any) -> bool:
    if getattr(session, "channel", None) not in UNATTENDED_CHANNELS:
        return False
    # A project thread is made on the worker channel, but the user chats in it.
    return not is_project_thread(getattr(session, "config", None))


def capture_schedule_context(session_config: dict | None, channel: str | None) -> dict[str, Any]:
    """The context a schedule keeps from the session that made it.

    Kept on the schedule so later changes to that chat (a different
    profile, the chat deleted) never change what its runs act as.
    """
    captured = {
        field: value
        for field, value in (session_config or {}).items()
        if field in SCHEDULE_FIELDS and field != "origin_channel"
    }
    origin = (session_config or {}).get("origin_channel") or channel
    if origin:
        captured["origin_channel"] = origin
    return captured


def inherit_context(
    parent_config: dict | None,
    parent_channel: str | None,
    captured: dict | None = None,
) -> dict[str, Any]:
    """The context fields a child session gets.

    *captured* (a schedule's) wins over the parent's current config, and
    alone carries an ``agent_type``.
    """
    if captured:
        return {field: captured[field] for field in SCHEDULE_FIELDS if field in captured}
    source = capture_schedule_context(parent_config, parent_channel)
    return {field: source[field] for field in CONTEXT_FIELDS if field in source}
