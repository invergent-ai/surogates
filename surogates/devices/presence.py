"""Which devices are connected, and messages to their connections.

Presence is ``surogates:device:{id}:online`` with a TTL.  Its value names the
one connection that may refresh or clear it, ``<pod>:<connection id>``.  A
new connection takes the key over and publishes ``superseded:<its holder>`` on
``surogates:device:{id}:control``, so an older connection closes whichever API
pod holds it.  Revocation (``revoked:<generation>``) and token rotation
(``rotated:<generation>``) are published on the same channel, where
``<generation>`` is the device's ``credential_generation``.
"""

from __future__ import annotations

import os
import uuid
from uuid import UUID

from redis.asyncio import Redis
from redis.asyncio.client import PubSub

HEARTBEAT_INTERVAL_S = 15
PRESENCE_TTL_S = 45

# Refresh our own claim, or an expired one: after a Redis restart the
# connection still holding the socket takes its presence back.  A key another
# connection took over is theirs.
_REFRESH = """
local current = redis.call('GET', KEYS[1])
if current == false or current == ARGV[1] then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
  return 1
end
return 0
"""

_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def presence_key(device_id: UUID) -> str:
    return f"surogates:device:{device_id}:online"


def control_channel(device_id: UUID) -> str:
    return f"surogates:device:{device_id}:control"


def new_holder() -> str:
    """A name for one connection: this pod, then a random connection id."""
    return f"{os.environ.get('HOSTNAME', 'api-local')}:{uuid.uuid4().hex}"


class DevicePresence:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def claim(self, device_id: UUID, holder: str) -> None:
        """Make *holder* the device's connection and tell any older one to close."""
        await self._redis.set(presence_key(device_id), holder, ex=PRESENCE_TTL_S)
        await self.publish(device_id, f"superseded:{holder}")

    async def refresh(self, device_id: UUID, holder: str) -> bool:
        """Extend *holder*'s claim.  False means another connection holds the device."""
        return bool(await self._redis.eval(
            _REFRESH, 1, presence_key(device_id), holder, PRESENCE_TTL_S,
        ))

    async def release(self, device_id: UUID, holder: str) -> None:
        """Clear the claim if *holder* still has it."""
        await self._redis.eval(_RELEASE, 1, presence_key(device_id), holder)

    async def holds(self, device_id: UUID, holder: str) -> bool:
        """Whether *holder* is the device's connection right now."""
        return await self._redis.get(presence_key(device_id)) == holder.encode()

    async def online(self, device_ids: list[UUID]) -> set[UUID]:
        if not device_ids:
            return set()
        values = await self._redis.mget([presence_key(d) for d in device_ids])
        return {d for d, value in zip(device_ids, values) if value is not None}

    async def publish(self, device_id: UUID, message: str) -> None:
        await self._redis.publish(control_channel(device_id), message)

    async def subscribe(self, device_id: UUID) -> PubSub:
        """A pub/sub connection whose subscription to the control channel Redis confirmed.

        ``PubSub.subscribe`` only sends the command; reading the confirmation
        makes "subscribed" true before a caller relies on it.
        """
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(control_channel(device_id))
        for _ in range(5):
            message = await pubsub.get_message(timeout=1.0)
            if message is not None and message["type"] == "subscribe":
                return pubsub
        await pubsub.aclose()
        raise ConnectionError("Redis did not confirm the control subscription")
