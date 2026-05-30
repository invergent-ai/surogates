"""Redis pub/sub listener that invalidates the shared-runtime caches.

Plan 1 / Task 17 + Plan 1b / Task 7.  Surogate-ops publishes on Redis
whenever an admin mutates an agent's runtime config, a file bundle
hub_ref, a project's Firebase config, or an agent's slug.  This module:

* :func:`handle_invalidation_message` — pure function that parses a
  channel name and dispatches to the matching cache.  Unit-testable
  without a real Redis connection.
* :func:`run_invalidator` — async coroutine that subscribes to the
  channels and forwards messages to the handler.  Started as a
  background task from the FastAPI lifespan.

Tolerance policy: a malformed publish (missing identifier, unknown
channel) is silently dropped rather than crashing the listener.  A bad
publisher must not be able to take down every shared-runtime pod
across the cluster.

Cache parameters are keyword-only and default to ``None`` so a pod that
has not wired (e.g.) the slug cache yet can still pass through the
runtime-config cache without re-plumbing.  ``None`` caches simply skip
dispatch for their channel prefix.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "INVALIDATION_CHANNELS",
    "handle_invalidation_message",
    "run_invalidator",
]


# (channel_prefix, cache-kwarg-name) — the kwarg points at which cache
# instance the handler invalidates when the channel matches.
_CHANNEL_ROUTING: tuple[tuple[str, str], ...] = (
    ("agent.runtime_config_changed:", "runtime_config_cache"),
    # File bundle hub_ref / version bumped (Plan 3 / Task 8).
    # Retargeted from runtime_config_cache (Plan 1b pre-routing) to
    # the new FileBundleCache.
    ("agent.bundle_changed:", "file_bundle_cache"),
    # Project Firebase config row changed (Plan 1b).
    ("project.firebase_config_changed:", "firebase_cache"),
    # Agent slug → agent_id mapping changed (Plan 1b Task 11).
    ("agent.slug_changed:", "slug_cache"),
    # Plan 4 / Task 2 — per-user memory invalidation.  Identifier
    # is ``<org_id>:<user_id>``; the MemoryCache keys on the same
    # colon-joined string so the channel identifier passes through
    # without a parser.
    ("user.memory_changed:", "memory_cache"),
    # Plan 5 / Task 7 — admin CRUD on the per-tenant MCP server
    # registry publishes here.  Identifier is just the agent_id
    # (no org_id prefix) because admins reference agents by id;
    # the MCPServerRegistryCache's get() composes the full
    # "<org_id>:<agent_id>" key from the caller's context, so the
    # invalidate() side keys on agent_id verbatim — which means a
    # rare cross-org collision in agent_id would over-invalidate,
    # not under-invalidate.  Agent ids are UUIDs in PROD; collision
    # probability is negligible.
    ("agent.mcp_servers_changed:", "mcp_server_cache"),
    # Plan 6 / Task 2 — channel routing rows mutated.  Identifier
    # shape is "<kind>:<identifier>" (e.g. "slack:A0123ABCD" or
    # "telegram:@my_bot") and the ChannelRoutingCache keys on the
    # same shape so the channel suffix passes through verbatim.
    ("channel_routing_changed:", "channel_routing_cache"),
)

INVALIDATION_CHANNELS: tuple[str, ...] = tuple(
    prefix for prefix, _ in _CHANNEL_ROUTING
)


def handle_invalidation_message(
    *,
    channel: str,
    payload: bytes,
    runtime_config_cache: Any = None,
    firebase_cache: Any = None,
    slug_cache: Any = None,
    file_bundle_cache: Any = None,
    memory_cache: Any = None,
    mcp_server_cache: Any = None,
    channel_routing_cache: Any = None,
) -> None:
    """Drop a single cache entry if the channel matches.

    Pure function — no Redis dependency.  ``payload`` is unused
    today (channel name carries the identifier) but is part of the
    signature so future schema changes can pass along context
    (e.g. the new ``version`` after the update) without breaking
    callers.
    """
    caches = {
        "runtime_config_cache": runtime_config_cache,
        "firebase_cache": firebase_cache,
        "slug_cache": slug_cache,
        "file_bundle_cache": file_bundle_cache,
        "memory_cache": memory_cache,
        "mcp_server_cache": mcp_server_cache,
        "channel_routing_cache": channel_routing_cache,
    }
    for prefix, cache_kwarg in _CHANNEL_ROUTING:
        if channel.startswith(prefix):
            identifier = channel[len(prefix):]
            cache = caches.get(cache_kwarg)
            if identifier and cache is not None:
                cache.invalidate(identifier)
            return


async def run_invalidator(
    redis: Any,
    *,
    runtime_config_cache: Any = None,
    firebase_cache: Any = None,
    slug_cache: Any = None,
    file_bundle_cache: Any = None,
    memory_cache: Any = None,
    mcp_server_cache: Any = None,
    channel_routing_cache: Any = None,
) -> None:
    """Long-running listener for runtime-config / firebase / slug invalidations.

    Wired from the FastAPI lifespan as a background task.  Iterates
    messages from ``redis.pubsub()`` and dispatches each to
    :func:`handle_invalidation_message`.  Yields control between
    messages so cancellation propagates cleanly when the lifespan
    shuts down the task on app exit.
    """
    pubsub = redis.pubsub()
    try:
        for prefix in INVALIDATION_CHANNELS:
            await pubsub.psubscribe(f"{prefix}*")
        async for msg in pubsub.listen():
            if msg.get("type") != "pmessage":
                continue
            channel = msg.get("channel")
            if isinstance(channel, bytes):
                channel = channel.decode()
            payload = msg.get("data") or b""
            handle_invalidation_message(
                channel=channel or "",
                payload=payload,
                runtime_config_cache=runtime_config_cache,
                firebase_cache=firebase_cache,
                slug_cache=slug_cache,
                file_bundle_cache=file_bundle_cache,
                memory_cache=memory_cache,
                mcp_server_cache=mcp_server_cache,
                channel_routing_cache=channel_routing_cache,
            )
    finally:
        try:
            await pubsub.aclose()
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass
