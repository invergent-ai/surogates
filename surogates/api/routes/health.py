"""Health and readiness probes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from surogates.health import infrastructure_readiness

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
    session_factory = getattr(request.app.state, "session_factory", None)
    redis = getattr(request.app.state, "redis", None)

    if session_factory is None or redis is None:
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "checks": {
                    "database": "ok" if session_factory is not None else "not configured",
                    "redis": "ok" if redis is not None else "not configured",
                },
            },
        )

    checks = await infrastructure_readiness(redis, session_factory)
    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
    )
