"""Redis-backed cross-session provider rate-limit guard."""

from __future__ import annotations

import logging
import re
import time
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_KEY_PREFIX = "provider"
_DEFAULT_TTL_SECONDS = 300


class ProviderRateLimitGuard:
    """Share provider 429 cooldowns across sessions through Redis."""

    def __init__(self, redis, provider: str) -> None:  # noqa: ANN001
        self._redis = redis
        self._provider = _normalize_provider(provider)
        self._key = f"{_KEY_PREFIX}:{self._provider}:rate_limit_until"

    async def remaining_seconds(self) -> float | None:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(self._key)
            if raw is None:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            reset_at = float(raw)
            remaining = reset_at - time.time()
            if remaining <= 0:
                await self._redis.delete(self._key)
                return None
            return remaining
        except Exception as exc:
            logger.debug("Provider rate-limit guard read failed: %s", exc)
            return None

    async def record_until(self, reset_at: float) -> None:
        if self._redis is None:
            return
        ttl = max(1, int(reset_at - time.time()))
        try:
            await self._redis.setex(self._key, ttl, str(float(reset_at)))
        except Exception as exc:
            logger.debug("Provider rate-limit guard write failed: %s", exc)

    async def record_cooldown(self, seconds: float | None) -> None:
        cooldown = seconds if seconds and seconds > 0 else _DEFAULT_TTL_SECONDS
        await self.record_until(time.time() + cooldown)


def _normalize_provider(provider: str) -> str:
    raw = str(provider or "unknown").strip().lower()
    parsed = urlparse(raw)
    if parsed.netloc:
        raw = parsed.netloc
    raw = raw.removeprefix("www.")
    return re.sub(r"[^a-z0-9_.:-]+", "_", raw)[:120] or "unknown"
