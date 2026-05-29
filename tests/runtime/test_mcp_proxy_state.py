"""Tests for shared-runtime plumbing on the MCP proxy app.

Plan 5 / Task 1.  Mirrors the api side's
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


def test_call_tool_route_has_agent_runtime_context_dep():
    """Plan 5 / Task 2 source-level regression: call_tool depends
    on agent_runtime_context_dep so the per-request agent context
    is resolved before the call dispatch."""
    import inspect
    import surogates.mcp_proxy.routes as routes

    src = inspect.getsource(routes.call_tool)
    assert "agent_runtime_context_dep" in src
    assert "AgentRuntimeContext" in src


def test_call_tool_route_has_rate_limit_dep():
    """Plan 5 / Task 3 source-level regression: the call entry
    point has rate_limit_dep so per-tenant MCP call budgets are
    enforced.  tools/list stays unrated — it's a metadata probe."""
    import inspect
    import surogates.mcp_proxy.routes as routes

    call_src = inspect.getsource(routes.call_tool)
    assert "rate_limit_dep" in call_src

    list_src = inspect.getsource(routes.list_tools)
    assert "rate_limit_dep" not in list_src


@pytest.mark.asyncio
async def test_install_proxy_plumbing_helm_mode_skips():
    """Helm-mode proxy pods don't run the shared-runtime path;
    the helpers must be no-ops so the proxy still boots."""
    from surogates.mcp_proxy.app import (
        _install_shared_runtime_plumbing_for_proxy,
        _shutdown_shared_runtime_plumbing_for_proxy,
    )

    app = FastAPI()
    app.state.redis = _FakeRedis()
    _install_shared_runtime_plumbing_for_proxy(
        app, _make_settings(runtime_mode="helm"),
    )
    try:
        assert app.state.platform_client is None
        assert app.state.runtime_config_cache is None
        assert app.state.rate_limiter is None
    finally:
        await _shutdown_shared_runtime_plumbing_for_proxy(app)


@pytest.mark.asyncio
async def test_install_proxy_plumbing_empty_url_skips():
    """Shared mode + empty platform_api_url logs and stays None so
    a misconfigured pod still boots (the routes 503 / 429 on the
    first request instead of crashing at startup)."""
    from surogates.mcp_proxy.app import (
        _install_shared_runtime_plumbing_for_proxy,
        _shutdown_shared_runtime_plumbing_for_proxy,
    )

    app = FastAPI()
    app.state.redis = _FakeRedis()
    _install_shared_runtime_plumbing_for_proxy(
        app, _make_settings(platform_api_url=""),
    )
    try:
        assert app.state.platform_client is None
        assert app.state.runtime_config_cache is None
        assert app.state.rate_limiter is None
    finally:
        await _shutdown_shared_runtime_plumbing_for_proxy(app)
