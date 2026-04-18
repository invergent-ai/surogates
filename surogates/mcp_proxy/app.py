"""FastAPI application factory for the MCP proxy service."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from surogates.audit import AuditStore
from surogates.db.engine import async_engine_from_settings, async_session_factory
from surogates.mcp_proxy.config import load_proxy_settings
from surogates.mcp_proxy.pool import ConnectionPool
from surogates.tenant.credentials import CredentialVault

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage MCP proxy startup and shutdown resources."""
    settings = load_proxy_settings()
    engine = async_engine_from_settings(settings.db)

    app.state.session_factory = async_session_factory(engine)
    app.state.platform_mcp_dir = settings.platform_mcp_dir

    # Credential vault — requires a Fernet encryption key.
    if settings.encryption_key:
        app.state.vault = CredentialVault(
            app.state.session_factory,
            encryption_key=settings.encryption_key.encode("utf-8"),
        )
    else:
        logger.warning(
            "No encryption key configured — credential resolution disabled. "
            "Set SUROGATES_ENCRYPTION_KEY to enable.",
        )
        app.state.vault = _NoOpVault()

    # Audit substrate.  Each pool entry gets its own
    # :class:`MCPGovernance` instance for tenant-scoped fingerprints.
    app.state.audit_store = AuditStore(app.state.session_factory)

    # Connection pool.  Scan + audit wired in so every MCP tool
    # advertised to an agent has a safety scan recorded.
    pool = ConnectionPool(
        idle_timeout=settings.idle_connection_timeout,
        max_per_org=settings.max_connections_per_org,
        governance_enabled=True,
        audit_store=app.state.audit_store,
    )
    app.state.pool = pool
    pool.start_eviction_loop()

    logger.info(
        "MCP Proxy started (host=%s, port=%d, idle_timeout=%ds)",
        settings.host, settings.port, settings.idle_connection_timeout,
    )

    yield

    # Shutdown.
    logger.info("MCP Proxy shutting down")
    await pool.shutdown()
    await engine.dispose()


class _NoOpVault:
    """Stub vault used when no encryption key is configured."""

    async def retrieve(self, *args, **kwargs):
        return None


def create_app() -> FastAPI:
    """Build and return the MCP proxy FastAPI application."""
    app = FastAPI(
        title="Surogates MCP Proxy",
        description="Credential-injecting proxy for MCP tool calls",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Health check.
    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # MCP proxy routes.
    from surogates.mcp_proxy.routes import router

    app.include_router(router)

    return app
