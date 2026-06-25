"""Generic per-event tenant resolver over :class:`ChannelRoutingCache`.

Looks up ``"<kind>:<identifier>"`` in the cache once and returns a
normalised tenant dict, or ``None`` on a miss.  The cache already
handles TTL, negative memoisation, and per-key locking — this module
is a thin, pure wrapper.
"""

from __future__ import annotations

__all__ = ["resolve_tenant"]


async def resolve_tenant(
    cache,
    kind: str,
    identifier: str,
) -> dict | None:
    """Return the routing tenant for ``(kind, identifier)``, or ``None``.

    Parameters
    ----------
    cache:
        A :class:`~surogates.runtime.channel_routing_cache.ChannelRoutingCache`
        (or any object with an ``async get(key: str) -> dict | None`` method).
    kind:
        Channel type string, e.g. ``"slack"``, ``"telegram"``, ``"website"``.
    identifier:
        Channel-specific identifier, e.g. a Slack App-ID, a Telegram bot
        username, or a website public key.

    Returns
    -------
    dict
        ``{"org_id": ..., "agent_id": ..., "config": {...}}`` plus
        ``"api_web_url"`` when the routing record carries it.
    None
        The routing cache has no record for this ``(kind, identifier)`` pair
        (negative-memoised miss or genuinely unknown identifier).
    """
    routing = await cache.get(f"{kind}:{identifier}")
    if routing is None:
        return None

    result: dict = {
        "org_id": routing["org_id"],
        "agent_id": routing["agent_id"],
        "config": routing.get("config") or {},
    }
    if "api_web_url" in routing:
        result["api_web_url"] = routing["api_web_url"]
    return result
