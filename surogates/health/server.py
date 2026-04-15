"""Minimal side-car HTTP server for liveness and readiness probes."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)

ReadinessCheck = Callable[[], Awaitable[dict[str, str]]]


def _build_app(readiness_check: ReadinessCheck) -> Starlette:
    async def liveness(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def readiness(_: Request) -> JSONResponse:
        try:
            checks = await readiness_check()
        except Exception as exc:
            logger.warning("Readiness check raised: %s", exc)
            return JSONResponse(
                {"status": "error", "detail": str(exc)},
                status_code=503,
            )

        healthy = all(v == "ok" for v in checks.values())
        return JSONResponse(
            {"status": "ok" if healthy else "degraded", "checks": checks},
            status_code=200 if healthy else 503,
        )

    return Starlette(
        routes=[
            Route("/health", liveness),
            Route("/health/ready", readiness),
        ]
    )


class HealthServer:
    """Background uvicorn server exposing ``/health`` and ``/health/ready``."""

    def __init__(
        self,
        port: int,
        readiness_check: ReadinessCheck,
        host: str = "0.0.0.0",
    ) -> None:
        self._config = uvicorn.Config(
            _build_app(readiness_check),
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        self._server = uvicorn.Server(self._config)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._server.serve(), name="health-server")
        logger.info(
            "Health server listening on %s:%d", self._config.host, self._config.port,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._server.should_exit = True
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None


async def start_health_server(
    port: int,
    readiness_check: ReadinessCheck,
    host: str = "0.0.0.0",
) -> HealthServer:
    """Convenience factory that constructs and starts a :class:`HealthServer`."""
    server = HealthServer(port, readiness_check, host=host)
    await server.start()
    return server
