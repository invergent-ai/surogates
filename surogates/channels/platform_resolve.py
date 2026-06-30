"""Resolve a session's effective channel platform.

A single source of truth for the "ambient sessions act on Slack" rule so the
worker, the harness tool filter, and the api routes never drift apart.
"""

from __future__ import annotations

from typing import Any


def effective_channel_platform(session: Any) -> str:
    """Return the platform kind a session's channel I/O runs under.

    Ambient sessions are Slack-context but carry ``channel="ambient"``; their
    settings, channel recall, and native channel tools all live under the
    ``"slack"`` platform key. Returns the platform kind (e.g. ``"slack"`` or
    ``"telegram"``) or ``""`` when the session has no channel set.
    """
    channel = getattr(session, "channel", None)
    if channel == "ambient":
        return "slack"
    return channel or ""
