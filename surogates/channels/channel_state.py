"""Redis-backed channel adapter state.

The shared channel adapter runs as one (or more) pods serving many workspaces.
Session-key routing and mention-tracking must survive pod restarts and be
consistent across replicas, so they live in Redis rather than per-process
sets.  Keys are agent-scoped, platform-scoped, and TTL-bounded (default 7
days) which also replaces the old manual size-trim of the in-memory structures.
"""

from __future__ import annotations

from typing import Any

_DEFAULT_TTL = 7 * 24 * 3600  # 7 days


class ChannelAdapterState:
    """Durable per-agent, per-platform routing/mention state."""

    def __init__(
        self,
        redis: Any,
        *,
        agent_id: str,
        platform: str,
        ttl_seconds: int = _DEFAULT_TTL,
    ) -> None:
        self._redis = redis
        self._agent_id = agent_id
        self._platform = platform
        self._ttl = ttl_seconds

    def _session_key(self, session_key: str) -> str:
        return f"mate:{self._platform}:sessionmap:{self._agent_id}:{session_key}"

    def _mentioned_key(self, thread_ts: str) -> str:
        return f"mate:{self._platform}:mentioned:{self._agent_id}:{thread_ts}"

    def _botmsg_key(self, ts: str) -> str:
        return f"mate:{self._platform}:botmsg:{self._agent_id}:{ts}"

    async def remember_session(self, session_key: str, session_id: str) -> None:
        await self._redis.set(
            self._session_key(session_key), str(session_id), ex=self._ttl,
        )

    async def get_session(self, session_key: str) -> str | None:
        raw = await self._redis.get(self._session_key(session_key))
        if raw is None:
            return None
        return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)

    async def mark_mentioned_thread(self, thread_ts: str) -> None:
        await self._redis.set(self._mentioned_key(thread_ts), "1", ex=self._ttl)

    async def is_mentioned_thread(self, thread_ts: str) -> bool:
        return bool(await self._redis.exists(self._mentioned_key(thread_ts)))

    async def mark_bot_message(self, ts: str) -> None:
        await self._redis.set(self._botmsg_key(ts), "1", ex=self._ttl)

    async def is_bot_message(self, ts: str) -> bool:
        return bool(await self._redis.exists(self._botmsg_key(ts)))
