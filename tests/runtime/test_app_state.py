"""Tests for shared-runtime plumbing on the FastAPI app.state.

Plan 1 / Task 16.  Verifies the lifespan hook constructs / shuts down
the PlatformClient + RuntimeConfigCache exactly when
``runtime_mode='shared'`` and ``platform_api_url`` is configured.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI


def _make_settings(
    *,
    runtime_mode: str,
    platform_api_url: str = "https://ops.example.com",
    hub_endpoint: str = "",
):
    """A minimal settings-ish object that the lifespan helper accepts.

    We avoid constructing a full ``Settings()`` here because that
    triggers the pydantic-settings env scan (and the SUROGATES_CONFIG
    path resolution).  The helpers under test only read three
    attributes.
    """
    from types import SimpleNamespace

    return SimpleNamespace(
        runtime_mode=runtime_mode,
        platform_api_url=platform_api_url,
        hub=SimpleNamespace(
            endpoint=hub_endpoint, username="", password="",
        ),
        platform_api_token="t",
        api=SimpleNamespace(rate_limit_rpm=300),
    )


class _FakePubsub:
    """Async-iterable pub/sub stub.  The invalidator task subscribes
    and then iterates ``listen()``; we never emit a message so the
    task blocks until cancellation."""

    async def psubscribe(self, _pattern: str) -> None:
        return None

    async def aclose(self) -> None:
        return None

    def listen(self):
        async def _gen():
            import asyncio

            await asyncio.Event().wait()
            yield  # pragma: no cover

        return _gen()


class _FakeRedis:
    def pubsub(self) -> _FakePubsub:
        return _FakePubsub()


@pytest.mark.asyncio
async def test_install_shared_plumbing_wires_client_and_cache():
    from surogates.api.app import (
        _install_shared_runtime_plumbing,
        _shutdown_shared_runtime_plumbing,
    )
    from surogates.runtime import PlatformClient, RuntimeConfigCache

    app = FastAPI()
    app.state.redis = _FakeRedis()
    settings = _make_settings(runtime_mode="shared")
    _install_shared_runtime_plumbing(app, settings)

    try:
        assert isinstance(app.state.platform_client, PlatformClient)
        assert isinstance(app.state.runtime_config_cache, RuntimeConfigCache)
        assert app.state.runtime_invalidator_task is not None
        assert not app.state.runtime_invalidator_task.done()
        # Plan 1b / Task 11 — slug cache lands alongside the rest.
        from surogates.runtime import PerTenantRateLimiter, SlugResolverCache

        assert isinstance(
            app.state.slug_resolver_cache, SlugResolverCache,
        )
        # Plan 1b / Task 14 — rate limiter wired by lifespan.
        assert isinstance(
            app.state.rate_limiter, PerTenantRateLimiter,
        )
    finally:
        await _shutdown_shared_runtime_plumbing(app)


@pytest.mark.asyncio
async def test_install_shared_plumbing_wires_firebase_cache():
    """Plan 1b / Task 8 regression: shared-mode lifespan now also
    constructs a FirebaseConfigCache backed by PlatformClient and
    exposes it on app.state.firebase_config_cache.
    """
    from surogates.api.app import (
        _install_shared_runtime_plumbing,
        _shutdown_shared_runtime_plumbing,
    )
    from surogates.runtime import FirebaseConfigCache

    app = FastAPI()
    app.state.redis = _FakeRedis()
    _install_shared_runtime_plumbing(app, _make_settings(runtime_mode="shared"))

    try:
        assert isinstance(
            app.state.firebase_config_cache, FirebaseConfigCache,
        )
    finally:
        await _shutdown_shared_runtime_plumbing(app)


def test_install_shared_plumbing_skips_when_url_empty():
    """An unconfigured shared-mode pod must NOT silently swallow the
    misconfig — the resolver fails on first request instead."""
    from surogates.api.app import _install_shared_runtime_plumbing

    app = FastAPI()
    settings = _make_settings(runtime_mode="shared", platform_api_url="")
    _install_shared_runtime_plumbing(app, settings)

    assert app.state.platform_client is None
    assert app.state.runtime_config_cache is None
    assert app.state.firebase_config_cache is None
    assert app.state.slug_resolver_cache is None
    assert app.state.rate_limiter is None


@pytest.mark.asyncio
async def test_shutdown_closes_platform_client_if_present():
    """``_shutdown_shared_runtime_plumbing`` closes the client and
    clears the state attribute so a hot-reload cannot reuse a dead
    AsyncClient.  Also cancels the invalidator task cleanly."""
    from surogates.api.app import (
        _install_shared_runtime_plumbing,
        _shutdown_shared_runtime_plumbing,
    )

    app = FastAPI()
    app.state.redis = _FakeRedis()
    _install_shared_runtime_plumbing(app, _make_settings(runtime_mode="shared"))
    assert app.state.platform_client is not None
    invalidator_task = app.state.runtime_invalidator_task
    assert invalidator_task is not None

    await _shutdown_shared_runtime_plumbing(app)
    assert app.state.platform_client is None
    assert app.state.runtime_invalidator_task is None
    assert invalidator_task.cancelled() or invalidator_task.done()


@pytest.mark.asyncio
async def test_install_shared_plumbing_wires_file_bundle_cache(monkeypatch):
    """Plan 3 / Task 9 — shared-mode lifespan now also constructs
    a FileBundleCache backed by HubBundleClient when HubSettings
    is configured.

    The Hub SDK (``surogate_hub_sdk``) is an optional install in the
    surogates wheel; CI envs without it should skip this test
    rather than fail.  The monkeypatch below provides a minimal
    fake SDK module so the wiring logic can be exercised regardless
    of CI install state."""
    import sys
    import types

    fake_sdk = types.ModuleType("surogate_hub_sdk")
    fake_sdk.Configuration = lambda **kw: kw

    class _FakeHubClient:
        def __init__(self, *, configuration):
            self.objects_api = object()
            self.config = configuration

    fake_sdk.HubClient = _FakeHubClient
    monkeypatch.setitem(sys.modules, "surogate_hub_sdk", fake_sdk)

    from surogates.api.app import (
        _install_shared_runtime_plumbing,
        _shutdown_shared_runtime_plumbing,
    )
    from surogates.runtime import FileBundleCache

    app = FastAPI()
    app.state.redis = _FakeRedis()
    _install_shared_runtime_plumbing(
        app,
        _make_settings(
            runtime_mode="shared", hub_endpoint="https://hub",
        ),
    )
    try:
        assert isinstance(app.state.file_bundle_cache, FileBundleCache)
    finally:
        await _shutdown_shared_runtime_plumbing(app)


@pytest.mark.asyncio
async def test_install_shared_plumbing_file_bundle_cache_none_when_hub_disabled():
    """When SUROGATES_HUB_ENDPOINT is empty (legacy / on-prem mode),
    the cache stays None so the worker falls back to filesystem
    reads (Plan 9 retires this path)."""
    from surogates.api.app import (
        _install_shared_runtime_plumbing,
        _shutdown_shared_runtime_plumbing,
    )

    app = FastAPI()
    app.state.redis = _FakeRedis()
    _install_shared_runtime_plumbing(
        app,
        _make_settings(runtime_mode="shared", hub_endpoint=""),
    )
    try:
        assert app.state.file_bundle_cache is None
    finally:
        await _shutdown_shared_runtime_plumbing(app)


@pytest.mark.asyncio
async def test_shutdown_is_safe_when_no_client():
    from surogates.api.app import _shutdown_shared_runtime_plumbing

    app = FastAPI()
    app.state.platform_client = None
    # Must not raise.
    await _shutdown_shared_runtime_plumbing(app)
