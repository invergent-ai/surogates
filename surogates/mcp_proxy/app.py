"""FastAPI application factory for the MCP proxy service."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from redis.asyncio import Redis

from surogates.audit import AuditStore
from surogates.db.engine import async_engine_from_settings, async_session_factory
from surogates.mcp_proxy.config import McpProxySettings, load_proxy_settings
from surogates.mcp_proxy.pool import ConnectionPool
from surogates.tenant.credentials import CredentialVault

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage MCP proxy startup and shutdown resources."""
    settings = load_proxy_settings()
    engine = async_engine_from_settings(settings.db)

    app.state.session_factory = async_session_factory(engine)

    # Redis client for the rate limiter +
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
    # :class:`MCPGovernance` instance over its tenant's approved
    # fingerprints, which the pool keeps across connections.
    app.state.audit_store = AuditStore(app.state.session_factory)

    pool = _connection_pool(settings, app.state.audit_store)
    app.state.pool = pool
    pool.start_eviction_loop()

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


def _connection_pool(settings: McpProxySettings, audit_store: AuditStore) -> ConnectionPool:
    """The proxy's MCP connection pool.

    Scan + audit wired in, so every MCP tool advertised to an agent has a
    safety scan recorded, unless the deployment switched the scan off
    (``mcp_governance_enabled``): then it says so at startup (once per
    worker process), and ``/health`` says ``"mcp_scan": "off"``.
    """
    if not settings.mcp_governance_enabled:
        logger.warning(
            "MCP Proxy runs without the MCP security scan "
            "(SUROGATES_MCP_GOVERNANCE_ENABLED=false): tools are advertised "
            "unscanned, and a definition changed after its approval is not "
            "blocked. For test and benchmark deployments only.",
        )
    return ConnectionPool(
        idle_timeout=settings.idle_connection_timeout,
        max_per_org=settings.max_connections_per_org,
        governance_enabled=settings.mcp_governance_enabled,
        audit_store=audit_store,
        scan_wording=settings.mcp_scan_wording,
    )


class _NoOpVault:
    """Stub vault used when no encryption key is configured."""

    async def retrieve(self, *args, **kwargs):
        return None


def _install_shared_runtime_plumbing_for_proxy(app, settings) -> None:
    """Wire shared-runtime building blocks the proxy routes need.

    Trimmed version of the api-side
    :func:`surogates.api.app._install_shared_runtime_plumbing` —
    the proxy only needs ``PlatformClient`` + ``RuntimeConfigCache``
    (so :func:`agent_runtime_context_dep` resolves the per-request
    context) and ``PerTenantRateLimiter`` (so
    :func:`rate_limit_dep` gates the call entry).  File-bundle,
    memory, firebase, and slug caches are session-time concerns
    the worker handles.

    Requires ``settings.platform_api_url``; missing it makes
    ``agent_runtime_context_dep`` fail on every request — surface
    that at boot rather than silently degrade.
    """
    import asyncio

    from surogates.runtime import (
        PerTenantRateLimiter, PlatformClient,
        RuntimeConfigCache, run_invalidator,
    )

    if not settings.platform_api_url:
        raise RuntimeError(
            "SUROGATES_PLATFORM_API_URL is required; the MCP proxy "
            "cannot resolve per-tenant context without it",
        )

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

    app.state.platform_client = client
    app.state.runtime_config_cache = cache
    app.state.rate_limiter = rate_limiter
    app.state.runtime_invalidator_task = asyncio.create_task(
        run_invalidator(
            app.state.redis,
            runtime_config_cache=cache,
            mcp_pool=getattr(app.state, "pool", None),
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
    async def health(request: Request):
        pool = getattr(request.app.state, "pool", None)
        if pool is not None and not pool.governance_enabled:
            # Seen by whoever checks the proxy, not only in its startup log.
            return {"status": "ok", "mcp_scan": "off"}
        return {"status": "ok"}

    # MCP proxy routes.
    from surogates.mcp_proxy.routes import router

    app.include_router(router)

    return app
