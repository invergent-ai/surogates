"""Redis-backed sliding-window rate limiter.

Uses a simple INCR + EXPIRE pattern: each key represents a one-minute
window keyed by the caller identity (e.g. user ID or IP).  When the
counter exceeds the configured maximum the request is rejected with 429.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from fastapi import FastAPI, Request, Response
    from redis.asyncio import Redis

    from surogates.config import Settings

logger = logging.getLogger(__name__)


class RateLimiter:
    """Sliding-window rate limiter backed by Redis INCR + EXPIRE."""

    def __init__(self, redis: Redis, *, requests_per_minute: int = 60) -> None:
        self._redis = redis
        self._requests_per_minute = requests_per_minute

    async def check(self, key: str) -> bool:
        """Return ``True`` if the request is allowed, ``False`` if rate-limited."""
        window = int(time.time()) // 60
        redis_key = f"surogates:rate:{key}:{window}"

        try:
            current = await self._redis.incr(redis_key)
            if current == 1:
                # First request in this window -- set TTL so the key auto-expires.
                await self._redis.expire(redis_key, 120)
            return current <= self._requests_per_minute
        except Exception:
            # If Redis is unavailable, fail open to avoid blocking all traffic.
            logger.warning("Rate limiter Redis error; failing open", exc_info=True)
            return True


def setup_rate_limit_middleware(app: FastAPI, settings: Settings) -> None:
    """Install a rate-limiting middleware on *app*.

    The middleware reads ``app.state.redis`` lazily so it can be attached
    before the lifespan creates the Redis client.
    """
    requests_per_minute = getattr(settings.api, "rate_limit_rpm", 120)

    @app.middleware("http")
    async def _rate_limit_middleware(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        redis: Redis | None = getattr(request.app.state, "redis", None)
        if redis is None:
            # Redis not yet initialised (e.g. during startup probes).
            return await call_next(request)

        # Use the authenticated user ID when available, otherwise fall back
        # to the client IP.
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            # Derive a stable key from the token's first 32 chars to avoid
            # storing the full JWT in Redis.
            key = f"token:{auth_header[7:39]}"
        else:
            key = f"ip:{request.client.host if request.client else 'unknown'}"

        limiter = RateLimiter(redis, requests_per_minute=requests_per_minute)
        if not await limiter.check(key):
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Try again later."},
                headers={"Retry-After": "60"},
            )

        return await call_next(request)
