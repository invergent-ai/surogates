"""Tests for shared-runtime plumbing on the MCP proxy app.

Mirrors the api side's
``_install_shared_runtime_plumbing`` so ``rate_limit_dep`` and
``agent_runtime_context_dep`` work on the proxy routes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI


def _make_settings(
    *,
    runtime_mode: str = "shared",
    platform_api_url: str = "https://ops.example.com",
):
    return SimpleNamespace(
        runtime_mode=runtime_mode,
        platform_api_url=platform_api_url,
        platform_api_token="t",
        api=SimpleNamespace(rate_limit_rpm=300),
    )


class _FakePubsub:
    async def psubscribe(self, _pattern):
        return None

    async def aclose(self):
        return None

    def listen(self):
        async def _gen():
            import asyncio
            await asyncio.Event().wait()
            yield  # pragma: no cover
        return _gen()


class _FakeRedis:
    def pubsub(self):
        return _FakePubsub()


@pytest.mark.asyncio
async def test_install_proxy_plumbing_wires_runtime_cache_and_limiter():
    """The proxy app gets the same RuntimeConfigCache +
    PerTenantRateLimiter pair as the api app so per-request
    runtime context + rate limit decisions are made from the
    same authoritative source."""
    from surogates.mcp_proxy.app import (
        _install_shared_runtime_plumbing_for_proxy,
        _shutdown_shared_runtime_plumbing_for_proxy,
    )
    from surogates.runtime import (
        PerTenantRateLimiter, PlatformClient, RuntimeConfigCache,
    )

    app = FastAPI()
    app.state.redis = _FakeRedis()
    _install_shared_runtime_plumbing_for_proxy(app, _make_settings())
    try:
        assert isinstance(app.state.platform_client, PlatformClient)
        assert isinstance(
            app.state.runtime_config_cache, RuntimeConfigCache,
        )
        assert isinstance(app.state.rate_limiter, PerTenantRateLimiter)
    finally:
        await _shutdown_shared_runtime_plumbing_for_proxy(app)


@pytest.mark.asyncio
async def test_install_proxy_plumbing_empty_url_raises():
    """The proxy needs platform_api_url to resolve AgentRuntimeContext.

    Missing it fails at startup instead of booting a pod whose MCP routes
    cannot resolve agent context.
    """
    from surogates.mcp_proxy.app import _install_shared_runtime_plumbing_for_proxy

    app = FastAPI()
    app.state.redis = _FakeRedis()
    with pytest.raises(RuntimeError, match="SUROGATES_PLATFORM_API_URL"):
        _install_shared_runtime_plumbing_for_proxy(
            app, _make_settings(platform_api_url=""),
        )
