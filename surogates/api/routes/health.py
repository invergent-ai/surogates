"""Health and readiness probes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    """Liveness probe -- always returns 200 if the process is running."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(request: Request) -> JSONResponse:
    """Readiness probe -- verifies DB and Redis connectivity.

    Returns 200 when both dependencies are reachable, 503 otherwise.
    """
    checks: dict[str, str] = {}
    healthy = True

    # -- Database --------------------------------------------------------
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is not None:
        try:
            async with session_factory() as session:
                await session.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:
            logger.warning("Readiness: database check failed: %s", exc)
            checks["database"] = f"error: {exc}"
            healthy = False
    else:
        checks["database"] = "not configured"
        healthy = False

    # -- Redis -----------------------------------------------------------
    redis = getattr(request.app.state, "redis", None)
    if redis is not None:
        try:
            await redis.ping()
            checks["redis"] = "ok"
        except Exception as exc:
            logger.warning("Readiness: redis check failed: %s", exc)
            checks["redis"] = f"error: {exc}"
            healthy = False
    else:
        checks["redis"] = "not configured"
        healthy = False

    status_code = 200 if healthy else 503
    return JSONResponse(
        status_code=status_code,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
    )
