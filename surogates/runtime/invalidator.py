"""Redis pub/sub listener that invalidates the shared-runtime caches.

Surogate-ops publishes on Redis
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
    # File bundle hub_ref / version bumped.
    # Retargeted from runtime_config_cache to
    # the new FileBundleCache.
    ("agent.bundle_changed:", "file_bundle_cache"),
    # Project Firebase config row changed.
    ("project.firebase_config_changed:", "firebase_cache"),
    # Agent slug → agent_id mapping changed.
    ("agent.slug_changed:", "slug_cache"),
    # per-user memory invalidation.  Identifier
    # is ``<org_id>:<user_id>``; the MemoryCache keys on the same
    # colon-joined string so the channel identifier passes through
    # without a parser.
    ("user.memory_changed:", "memory_cache"),
    # admin attach/detach of MCP servers on an agent.  Identifier is
    # the agent_id.  Refreshes the runtime config (so ctx.mcp_server_ids
    # updates) and evicts the agent's proxy pool entry (see
    # _POOL_INVALIDATION_PREFIXES below).
    ("agent.mcp_servers_changed:", "runtime_config_cache"),
    # channel routing rows mutated.  Identifier
    # shape is "<kind>:<identifier>" (e.g. "slack:A0123ABCD" or
    # "telegram:@my_bot") and the ChannelRoutingCache keys on the
    # same shape so the channel suffix passes through verbatim.
    ("channel_routing_changed:", "channel_routing_cache"),
    ("mate_settings_changed:", "mate_settings_cache"),
    # A channel sender linked their real account.  link_channel publishes
    # ``channel_identity_changed:<platform>\x00<platform_user_id>\x00<org_id>``
    # — the channels pod's per-message identity cache keys on that same
    # NUL-joined string, so the suffix passes through verbatim and the
    # just-linked user's negative-cache entry is evicted on bind rather than at
    # TTL expiry.
    ("channel_identity_changed:", "channel_identity_cache"),
    # An agent's service-account principal was rotated or revoked.  ops
    # publishes ``agent_principal_changed:<org_id>\x00<agent_id>``; the runtime
    # resolver cache keys on that same NUL-joined string, so the suffix passes
    # through verbatim.
    ("agent_principal_changed:", "agent_principal_cache"),
    # global system-skills bundle bumped.  The ops
    # `surogate-ops seed-builtin-skills` CLI publishes
    # ``system_skills_changed:<new_tag>`` after a successful
    # publish so every api / worker pod drops its cached
    # SystemBundleCache snapshot.  The cache ignores the
    # identifier (the next ``get()`` re-resolves the latest tag
    # from Hub on its own) but the dispatcher requires the
    # identifier suffix to be non-empty before it fires
    # ``invalidate()``.
    ("system_skills_changed:", "system_bundle_cache"),
)

# Channels whose identifier is an agent_id and that must also evict the
# MCP proxy's per-agent connection pool entry, so a detached server stops
# being callable at once rather than at the idle TTL.
_POOL_INVALIDATION_PREFIXES: frozenset[str] = frozenset({
    "agent.runtime_config_changed:",
    "agent.mcp_servers_changed:",
})


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
    mcp_pool: Any = None,
    channel_routing_cache: Any = None,
    system_bundle_cache: Any = None,
    mate_settings_cache: Any = None,
    channel_identity_cache: Any = None,
    agent_principal_cache: Any = None,
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
        "channel_routing_cache": channel_routing_cache,
        "system_bundle_cache": system_bundle_cache,
        "mate_settings_cache": mate_settings_cache,
        "channel_identity_cache": channel_identity_cache,
        "agent_principal_cache": agent_principal_cache,
    }
    for prefix, cache_kwarg in _CHANNEL_ROUTING:
        if channel.startswith(prefix):
            identifier = channel[len(prefix):]
            if not identifier:
                return
            cache = caches.get(cache_kwarg)
            if cache is not None:
                cache.invalidate(identifier)
            if mcp_pool is not None and prefix in _POOL_INVALIDATION_PREFIXES:
                mcp_pool.invalidate_agent(identifier)
            return


async def run_invalidator(
    redis: Any,
    *,
    runtime_config_cache: Any = None,
    firebase_cache: Any = None,
    slug_cache: Any = None,
    file_bundle_cache: Any = None,
    memory_cache: Any = None,
    mcp_pool: Any = None,
    channel_routing_cache: Any = None,
    system_bundle_cache: Any = None,
    mate_settings_cache: Any = None,
    channel_identity_cache: Any = None,
    agent_principal_cache: Any = None,
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
                mcp_pool=mcp_pool,
                channel_routing_cache=channel_routing_cache,
                system_bundle_cache=system_bundle_cache,
                mate_settings_cache=mate_settings_cache,
                channel_identity_cache=channel_identity_cache,
                agent_principal_cache=agent_principal_cache,
            )
    finally:
        try:
            await pubsub.aclose()
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass
