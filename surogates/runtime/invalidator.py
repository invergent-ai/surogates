"""Redis pub/sub listener that invalidates the runtime-config cache.

Plan 1 / Task 17.  Surogate-ops publishes on Redis whenever an admin
mutates an agent's runtime config (or, in Plan 3, an agent's bundle
hub_ref).  This module:

* :func:`handle_invalidation_message` — pure function that parses a
  channel name and calls ``cache.invalidate(agent_id)`` when the
  channel matches one of :data:`INVALIDATION_CHANNELS`.  Unit-testable
  without a real Redis connection.
* :func:`run_invalidator` — async coroutine that subscribes to the
  channels and dispatches messages to the handler.  Started as a
  background task from the FastAPI lifespan (Task 16).

Tolerance policy: a malformed publish (no agent_id, unknown channel)
is silently dropped rather than crashing the listener.  A bad
publisher must not be able to take down every shared-runtime pod
across the cluster.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "INVALIDATION_CHANNELS",
    "handle_invalidation_message",
    "run_invalidator",
]


INVALIDATION_CHANNELS: tuple[str, ...] = (
    # Runtime config row changed (Plan 1 / 7).
    "agent.runtime_config_changed:",
    # File bundle hub_ref bumped (Plan 3 — pre-routed here).
    "agent.bundle_changed:",
)


def handle_invalidation_message(
    cache: Any, *, channel: str, payload: bytes,
) -> None:
    """Drop a single cache entry if the channel matches.

    Pure function — no Redis dependency.  ``payload`` is unused
    today (channel name carries the agent_id) but is part of the
    signature so future schema changes can pass along context
    (e.g. the new ``version`` after the update) without breaking
    callers.
    """
    for prefix in INVALIDATION_CHANNELS:
        if channel.startswith(prefix):
            agent_id = channel[len(prefix):]
            if agent_id:
                cache.invalidate(agent_id)
            return


async def run_invalidator(redis: Any, cache: Any) -> None:
    """Long-running listener for runtime-config invalidations.

    Wired from the FastAPI lifespan in Task 16 as a background task.
    Iterates messages from ``redis.pubsub()`` and dispatches each to
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
                cache, channel=channel or "", payload=payload,
            )
    finally:
        try:
            await pubsub.aclose()
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass
