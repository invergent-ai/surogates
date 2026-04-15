"""Shared readiness checks for infrastructure dependencies."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import async_sessionmaker


async def _check_redis(redis_client: Redis) -> str:
    try:
        await redis_client.ping()
        return "ok"
    except Exception as exc:
        return f"error: {exc}"


async def _check_database(session_factory: async_sessionmaker) -> str:
    try:
        async with session_factory() as db:
            await db.execute(text("SELECT 1"))
        return "ok"
    except Exception as exc:
        return f"error: {exc}"


async def infrastructure_readiness(
    redis_client: Redis,
    session_factory: async_sessionmaker,
) -> dict[str, str]:
    """Run Redis and database liveness checks in parallel.

    Returns a ``{"redis": "ok" | error, "database": "ok" | error}`` dict
    suitable for the :mod:`surogates.health.server` readiness endpoint
    and the FastAPI ``/health/ready`` route.
    """
    redis_result, db_result = await asyncio.gather(
        _check_redis(redis_client),
        _check_database(session_factory),
    )
    return {"redis": redis_result, "database": db_result}
