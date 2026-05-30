"""FastAPI application factory for the MCP proxy service."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from redis.asyncio import Redis

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

    # Plan 5 / Task 1 — Redis client for the rate limiter +
    # pub/sub invalidator.  Created here (the api creates its own
    # in surogates.api.app) so the proxy can share the same
    # invalidation channels as the api + worker.
    app.state.redis = Redis.from_url(
        settings.redis.url, decode_responses=False,
    )

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

    # Plan 5 / Task 1 — shared-runtime plumbing on the proxy app.
    # No-op in helm mode or with empty platform_api_url.
    _install_shared_runtime_plumbing_for_proxy(app, settings)

    logger.info(
        "MCP Proxy started (host=%s, port=%d, idle_timeout=%ds)",
        settings.host, settings.port, settings.idle_connection_timeout,
    )

    yield

    # Shutdown.
    logger.info("MCP Proxy shutting down")
    await pool.shutdown()
    await _shutdown_shared_runtime_plumbing_for_proxy(app)
    await app.state.redis.aclose()
    await engine.dispose()


class _NoOpVault:
    """Stub vault used when no encryption key is configured."""

    async def retrieve(self, *args, **kwargs):
        return None


def _install_shared_runtime_plumbing_for_proxy(app, settings) -> None:
    """Wire shared-runtime building blocks the proxy routes need.

    Plan 5 / Task 1.  Trimmed version of the api-side
    :func:`surogates.api.app._install_shared_runtime_plumbing` —
    the proxy only needs ``PlatformClient`` + ``RuntimeConfigCache``
    (so :func:`agent_runtime_context_dep` resolves the per-request
    context) and ``PerTenantRateLimiter`` (so
    :func:`rate_limit_dep` gates the call entry).  File-bundle,
    memory, firebase, and slug caches are session-time concerns
    the worker handles.

    Helm mode + empty ``platform_api_url`` both leave the
    attributes as ``None`` so the proxy still boots; routes
    silently bypass the dep checks in those modes.
    """
    import asyncio

    from surogates.runtime import (
        MCPServerRegistryCache, PerTenantRateLimiter, PlatformClient,
        RuntimeConfigCache, run_invalidator,
    )

    if getattr(settings, "runtime_mode", "helm") != "shared":
        app.state.platform_client = None
        app.state.runtime_config_cache = None
        app.state.rate_limiter = None
        app.state.mcp_server_cache = None
        app.state.runtime_invalidator_task = None
        return

    if not settings.platform_api_url:
        logger.error(
            "runtime_mode='shared' but SUROGATES_PLATFORM_API_URL is empty; "
            "agent_runtime_context_dep will fail on every proxy request",
        )
        app.state.platform_client = None
        app.state.runtime_config_cache = None
        app.state.rate_limiter = None
        app.state.mcp_server_cache = None
        app.state.runtime_invalidator_task = None
        return

    client = PlatformClient(
        base_url=settings.platform_api_url,
        token=settings.platform_api_token,
    )
    cache = RuntimeConfigCache(
        loader=client.get_runtime_config, ttl_seconds=1.0,
    )
    rate_limiter = PerTenantRateLimiter(
        app.state.redis,
        default_rpm=getattr(settings.api, "rate_limit_rpm", 300),
    )

    async def _mcp_loader(agent_id: str) -> list[dict]:
        return await client.get_agent_mcp_servers(agent_id)

    mcp_server_cache = MCPServerRegistryCache(
        loader=_mcp_loader, ttl_seconds=30.0,
    )

    app.state.platform_client = client
    app.state.runtime_config_cache = cache
    app.state.rate_limiter = rate_limiter
    app.state.mcp_server_cache = mcp_server_cache
    app.state.runtime_invalidator_task = asyncio.create_task(
        run_invalidator(
            app.state.redis, runtime_config_cache=cache,
            mcp_server_cache=mcp_server_cache,
        ),
        name="surogates-mcp-proxy-runtime-invalidator",
    )


async def _shutdown_shared_runtime_plumbing_for_proxy(app) -> None:
    """Symmetric teardown for the proxy plumbing."""
    task = getattr(app.state, "runtime_invalidator_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except BaseException:  # noqa: BLE001 — cancellation expected
            pass
        app.state.runtime_invalidator_task = None

    client = getattr(app.state, "platform_client", None)
    if client is not None:
        await client.aclose()
        app.state.platform_client = None

    if hasattr(app.state, "runtime_config_cache"):
        app.state.runtime_config_cache = None
    if hasattr(app.state, "rate_limiter"):
        app.state.rate_limiter = None
    if hasattr(app.state, "mcp_server_cache"):
        app.state.mcp_server_cache = None


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
