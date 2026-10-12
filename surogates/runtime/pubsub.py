"""A Redis pub/sub listener that lasts as long as its process."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

__all__ = ["listen_forever"]

# Backoff (seconds) between subscribes after a Redis error; doubles up to the
# max and resets once a subscribe succeeds.
_RECONNECT_INITIAL_DELAY: float = 1.0
_RECONNECT_MAX_DELAY: float = 30.0


async def listen_forever(
    redis: Any,
    patterns: list[str],
    on_message: Callable[[dict], Awaitable[None]],
    *,
    name: str,
) -> None:
    """Hand every message published on *patterns* to *on_message*, until cancelled.

    A Redis error ends the subscription (a Valkey restart resets every
    connection), and a listener that ended with it would leave its process
    deaf to these channels until the pod restarted: it subscribes again,
    after a backoff.  A message *on_message* fails on is logged and skipped.
    """
    # ponytail: a half-open connection (Valkey's node lost without a reset)
    # raises nothing and keeps this deaf; add a PING liveness check if that bites.
    delay = _RECONNECT_INITIAL_DELAY
    while True:
        pubsub = redis.pubsub()
        try:
            await pubsub.psubscribe(*patterns)
            logger.info("%s subscribed to %s", name, ", ".join(patterns))
            delay = _RECONNECT_INITIAL_DELAY
            async for message in pubsub.listen():
                if message["type"] != "pmessage":
                    continue
                try:
                    await on_message(message)
                except Exception:
                    logger.exception("%s failed on a message on %s", name, message.get("channel"))
        except Exception:
            logger.error(
                "%s lost its Redis subscription; subscribing again in %.0fs", name, delay, exc_info=True,
            )
        finally:
            # Closing drops the subscription; on a dead connection it raises.
            with contextlib.suppress(Exception):
                await pubsub.aclose()
        await asyncio.sleep(delay)
        delay = min(delay * 2, _RECONNECT_MAX_DELAY)
