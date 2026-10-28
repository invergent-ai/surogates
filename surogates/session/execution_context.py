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

#: The channels a session made to help another runs on.
HELPER_CHANNELS: frozenset[str] = frozenset({"delegation", "worker", "task"})

#: ``ops``: the Studio operator and project the work is for (MCP ``_meta``,
#: owner-scoped reads).  ``operator``: the name the prompt gives the person.
#: ``browser``: the login profile.  ``origin_channel``: where the work began.
#: Every kind of child inherits all four.
CONTEXT_FIELDS: tuple[str, ...] = ("ops", "operator", "browser", "origin_channel")

#: A schedule also keeps which sub-agent made it: its runs act as that one,
#: with what it is offered and denied (``user_reports``, owner scope).  A
#: helper is not given its parent's: it names its own, or is the agent itself.
SCHEDULE_FIELDS: tuple[str, ...] = (*CONTEXT_FIELDS, "agent_type")


def channel_kind(channel: str | None, config: dict | None) -> str:
    """Who is behind a session: ``scheduled`` (a routine's run), ``helper``
    (delegation, worker, task) or ``person``: someone who can answer."""
    if channel == "scheduled" or (config or {}).get("scheduled_session_id"):
        return "scheduled"
    # A project thread is made on the worker channel, but the user chats in it.
    if channel in HELPER_CHANNELS and not is_project_thread(config):
        return "helper"
    return "person"


def session_kind(session: Any) -> str:
    return channel_kind(getattr(session, "channel", None), getattr(session, "config", None))


def is_unattended(session: Any) -> bool:
    return session_kind(session) != "person"


def is_scheduled_run(session: Any) -> bool:
    return session_kind(session) == "scheduled"


def platform_hint_channel(channel: str | None, config: dict | None) -> str | None:
    """Whose platform hint a session gets: a helper the delegation one, a
    project thread none (its worker channel's would say nobody is there)."""
    kind = channel_kind(channel, config)
    if kind == "helper":
        return "delegation"
    if kind == "person" and channel in HELPER_CHANNELS:
        return None
    return channel


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
    inherited = {field: source[field] for field in CONTEXT_FIELDS if field in source}
    # Logins are granted per channel, and no grant names a helper's own.
    login_channel = (parent_config or {}).get("login_channel") or parent_channel
    if login_channel:
        inherited["login_channel"] = login_channel
    return inherited
