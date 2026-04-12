"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from surogates.config import load_settings
from surogates.db.engine import async_engine_from_settings, async_session_factory
from surogates.session.store import SessionStore
from surogates.storage.backend import create_backend

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage application startup and shutdown resources."""
    # -- startup ----------------------------------------------------------
    settings = load_settings()
    engine = async_engine_from_settings(settings.db)

    app.state.settings = settings
    app.state.session_factory = async_session_factory(engine)
    app.state.redis = Redis.from_url(settings.redis.url)
    app.state.session_store = SessionStore(app.state.session_factory, redis=app.state.redis)
    app.state.storage = create_backend(settings)

    logger.info("Surogates API started (workers=%d)", settings.api.workers)

    yield

    # -- shutdown ---------------------------------------------------------
    logger.info("Surogates API shutting down")
    await app.state.redis.aclose()
    await engine.dispose()


def create_app() -> FastAPI:
    """Build and return the FastAPI application.

    Called by uvicorn as a factory (``uvicorn surogates.api.app:create_app --factory``).
    """
    settings = load_settings()

    app = FastAPI(
        title="Surogates",
        description="Managed Agents Platform",
        version="0.1.0",
        lifespan=lifespan,
    )

    # --- CORS ------------------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- structured logging ------------------------------------------------
    from surogates.logging_config import configure_logging

    configure_logging(level=settings.api.log_level if hasattr(settings.api, "log_level") else logging.INFO)

    # --- middleware -------------------------------------------------------
    from surogates.api.middleware.auth import setup_auth_middleware
    from surogates.api.middleware.rate_limit import setup_rate_limit_middleware
    from surogates.api.middleware.tenant import setup_tenant_middleware
    from surogates.api.middleware.trace import setup_trace_middleware

    # Trace middleware must be registered AFTER the others so that
    # Starlette executes it FIRST (outermost layer).
    setup_auth_middleware(app, settings)
    setup_tenant_middleware(app, settings)
    setup_rate_limit_middleware(app, settings)
    setup_trace_middleware(app, settings)

    # --- routes ----------------------------------------------------------
    from surogates.api.routes import admin, auth, events, health, memory, sessions, skills, tools, transparency, workspace

    app.include_router(health.router, tags=["health"])
    app.include_router(auth.router, prefix="/v1", tags=["auth"])
    app.include_router(sessions.router, prefix="/v1", tags=["sessions"])
    app.include_router(events.router, prefix="/v1", tags=["events"])
    app.include_router(tools.router, prefix="/v1", tags=["tools"])
    app.include_router(skills.router, prefix="/v1", tags=["skills"])
    app.include_router(memory.router, prefix="/v1", tags=["memory"])
    app.include_router(transparency.router, prefix="/v1", tags=["transparency"])
    app.include_router(workspace.router, prefix="/v1", tags=["workspace"])
    app.include_router(admin.router, prefix="/v1/admin", tags=["admin"])

    return app
