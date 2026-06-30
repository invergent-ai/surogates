"""Product gate for channel context-building ("follow the channel").

This ships a config-driven default; the ops settings layer replaces the body
with a lookup against the per-channel ``mate_channel_settings`` table (fetched
via the runtime settings cache).  The signature and import path are the stable
seam other modules depend on -- do not move them.
"""

from __future__ import annotations

from typing import Any

_FOLLOW_PLATFORMS = {"slack"}


def channel_follow_enabled(session: Any) -> bool:
    """Return True when firehose ingestion is enabled for this session."""
    channel = getattr(session, "channel", None)
    if channel not in _FOLLOW_PLATFORMS:
        return False
    config = getattr(session, "config", None) or {}
    return bool(config.get("mate_follow"))
