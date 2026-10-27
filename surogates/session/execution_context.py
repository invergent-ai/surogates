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

#: Sessions with nobody behind them to answer a question.
UNATTENDED_CHANNELS: frozenset[str] = frozenset({"scheduled", "delegation", "worker", "task"})

#: ``ops``: the Studio operator and project the work is for (MCP ``_meta``,
#: owner-scoped reads).  ``operator``: the name the prompt gives the person.
#: ``browser``: the login profile.  ``origin_channel``: where the work began.
#: Every kind of child inherits all four.
CONTEXT_FIELDS: tuple[str, ...] = ("ops", "operator", "browser", "origin_channel")


def is_unattended(session: Any) -> bool:
    return getattr(session, "channel", None) in UNATTENDED_CHANNELS


def capture_schedule_context(session_config: dict | None, channel: str | None) -> dict[str, Any]:
    """The context a schedule keeps from the session that made it.

    Kept on the schedule so later changes to that chat (a different
    profile, the chat deleted) never change what its runs act as.
    """
    captured = {
        field: value
        for field, value in (session_config or {}).items()
        if field in CONTEXT_FIELDS and field != "origin_channel"
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

    *captured* (a schedule's) wins over the parent's current config.
    """
    source = captured if captured else capture_schedule_context(parent_config, parent_channel)
    return {field: source[field] for field in CONTEXT_FIELDS if field in source}
