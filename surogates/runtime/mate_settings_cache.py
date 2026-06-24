"""Per-process TTL cache for Mate per-channel settings.

Same caching machinery as :class:`ChannelRoutingCache` (TTL, negative
memoisation, per-key locks); only the key shape and semantics differ.  The
cache key is ``"<agent_id>:<platform>:<channel_id>"``.

A ``"<agent_id>:<platform>:*"`` wildcard invalidation drops every cached
channel under an agent/platform -- used when the agent-level default row
changes, which affects every channel that inherits it.
"""

from __future__ import annotations

from surogates.runtime.channel_routing_cache import ChannelRoutingCache

__all__ = ["MateSettingsCache", "mate_cache_key"]


def mate_cache_key(agent_id: str, platform: str, channel_id: str) -> str:
    return f"{agent_id}:{platform}:{channel_id}"


class MateSettingsCache(ChannelRoutingCache):
    """TTL cache of effective Mate settings keyed by agent/platform/channel."""

    def invalidate(self, key: str) -> None:
        if key.endswith(":*"):
            prefix = key[:-1]
            for cached_key in list(self._entries.keys()):
                if cached_key.startswith(prefix):
                    self._entries.pop(cached_key, None)
            return
        super().invalidate(key)
