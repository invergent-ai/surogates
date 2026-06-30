"""Hard rate caps for ambient posts, enforced outside the model.

Confidence gates candidacy; these caps are mechanical and unbypassable.
All counters live in Redis so caps hold across worker replicas.
"""

from __future__ import annotations

from typing import Any

_DAY_SECONDS = 24 * 3600


class AmbientRateLimiter:
    def __init__(self, redis: Any) -> None:
        self._redis = redis

    def _count_key(self, agent_id: str, channel_id: str) -> str:
        return f"mate:ambient:postcount:{agent_id}:{channel_id}"

    def _last_key(self, agent_id: str, channel_id: str) -> str:
        return f"mate:ambient:lastpost:{agent_id}:{channel_id}"

    def _revive_key(self, agent_id: str, thread_ts: str) -> str:
        return f"mate:ambient:revive:{agent_id}:{thread_ts}"

    async def allow_post(
        self, *, agent_id: str, channel_id: str,
        max_per_day: int, min_seconds_between: int,
    ) -> bool:
        raw = await self._redis.get(self._count_key(agent_id, channel_id))
        count = int(raw) if raw is not None else 0
        if count >= max_per_day:
            return False
        if min_seconds_between > 0:
            last = await self._redis.get(self._last_key(agent_id, channel_id))
            if last is not None:
                # Presence within the min-gap TTL window means too-recent.
                return False
        return True

    async def record_post(self, *, agent_id: str, channel_id: str) -> None:
        ckey = self._count_key(agent_id, channel_id)
        await self._redis.incr(ckey)
        await self._redis.expire(ckey, _DAY_SECONDS)

    async def record_post_gap(
        self, *, agent_id: str, channel_id: str, min_seconds_between: int,
    ) -> None:
        if min_seconds_between > 0:
            await self._redis.set(
                self._last_key(agent_id, channel_id), "1",
                ex=min_seconds_between,
            )

    async def allow_revive(
        self, *, agent_id: str, thread_ts: str, window_seconds: int,
    ) -> bool:
        ok = await self._redis.set(
            self._revive_key(agent_id, thread_ts), "1",
            ex=window_seconds, nx=True,
        )
        return bool(ok)
