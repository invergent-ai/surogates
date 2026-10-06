"""How many calls the speech services can carry at once, shared by every voice worker.

A call holds an STT stream for its whole length (8 per STT pod), so calls beyond that would get an
STT error instead of a voice. Slots live in one Redis sorted set scored by when each hold expires:
a worker that dies mid-call cannot leak its slot past the call's time limit.
"""
from __future__ import annotations

import time
from typing import Any

# take a slot atomically: drop expired holds, then add this call only if there is room
_TAKE = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[2]) then
  redis.call('ZADD', KEYS[1], ARGV[3], ARGV[4])
  return 1
end
return 0
"""


class CallSlots:
    def __init__(self, redis: Any, capacity: int, key: str = "voice:call_slots") -> None:
        self._redis, self._capacity, self._key = redis, capacity, key

    async def take(self, call_id: str, *, hold_seconds: float) -> bool:
        """Hold a slot for ``call_id`` (at most ``hold_seconds``); False when every slot is taken."""
        now = time.time()
        return bool(await self._redis.eval(_TAKE, 1, self._key, now, self._capacity, now + hold_seconds, call_id))

    async def release(self, call_id: str) -> None:
        await self._redis.zrem(self._key, call_id)
