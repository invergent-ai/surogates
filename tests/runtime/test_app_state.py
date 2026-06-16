"""Tests for shared-runtime plumbing on the FastAPI app.state.

Verifies the lifespan hook constructs / shuts down
the PlatformClient + RuntimeConfigCache + bundle/memory caches.

The shared runtime is fail-loud: ``platform_api_url``, ``hub.endpoint``
and a configured ``storage.bucket`` are all mandatory.  A misconfigured
pod raises at boot rather than silently serving broken requests, so the
"success" tests below provide all three and the misconfig tests assert
the RuntimeError.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI


def _make_settings(
    *,
    platform_api_url: str = "https://ops.example.com",
    hub_endpoint: str = "https://hub",
    storage_bucket: str = "test-bucket",
):
    """A minimal settings-ish object that the lifespan helper accepts.

    We avoid constructing a full ``Settings()`` here because that
    triggers the pydantic-settings env scan (and the SUROGATES_CONFIG
    path resolution).  The helper only reads a handful of attributes.
    """
    from types import SimpleNamespace

    return SimpleNamespace(
        platform_api_url=platform_api_url,
        hub=SimpleNamespace(
            endpoint=hub_endpoint, username="", password="",
        ),
        storage=SimpleNamespace(
            bucket=storage_bucket, memory_bucket="",
        ),
        platform_api_token="t",
        api=SimpleNamespace(rate_limit_rpm=300),
    )


def _build_app() -> FastAPI:
    """A FastAPI app wired with the state the helper reads: a redis
    stub (for the rate limiter + invalidator) and a storage sentinel
    (so ``build_memory_cache`` sees a configured backend)."""
    app = FastAPI()
    app.state.redis = _FakeRedis()
    app.state.storage = object()
    return app


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

    app = _build_app()
    _install_shared_runtime_plumbing(app, _make_settings())

    try:
        assert isinstance(app.state.platform_client, PlatformClient)
        assert isinstance(app.state.runtime_config_cache, RuntimeConfigCache)
        assert app.state.runtime_invalidator_task is not None
        assert not app.state.runtime_invalidator_task.done()
        # slug cache lands alongside the rest.
        from surogates.runtime import PerTenantRateLimiter, SlugResolverCache

        assert isinstance(
            app.state.slug_resolver_cache, SlugResolverCache,
        )
        # rate limiter wired by lifespan.
        assert isinstance(
            app.state.rate_limiter, PerTenantRateLimiter,
        )
    finally:
        await _shutdown_shared_runtime_plumbing(app)


@pytest.mark.asyncio
async def test_install_shared_plumbing_wires_firebase_cache():
    """shared-mode lifespan now also
    constructs a FirebaseConfigCache backed by PlatformClient and
    exposes it on app.state.firebase_config_cache.
    """
    from surogates.api.app import (
        _install_shared_runtime_plumbing,
        _shutdown_shared_runtime_plumbing,
    )
    from surogates.runtime import FirebaseConfigCache

    app = _build_app()
    _install_shared_runtime_plumbing(app, _make_settings())

    try:
        assert isinstance(
            app.state.firebase_config_cache, FirebaseConfigCache,
        )
    finally:
        await _shutdown_shared_runtime_plumbing(app)


def test_install_shared_plumbing_raises_when_url_empty():
    """An unconfigured shared-mode pod must NOT silently swallow the
    misconfig — the helper raises at boot so the pod never serves
    broken requests."""
    from surogates.api.app import _install_shared_runtime_plumbing

    app = _build_app()
    settings = _make_settings(platform_api_url="")
    with pytest.raises(RuntimeError, match="SUROGATES_PLATFORM_API_URL"):
        _install_shared_runtime_plumbing(app, settings)


@pytest.mark.asyncio
async def test_shutdown_closes_platform_client_if_present():
    """``_shutdown_shared_runtime_plumbing`` closes the client and
    clears the state attribute so a hot-reload cannot reuse a dead
    AsyncClient.  Also cancels the invalidator task cleanly."""
    from surogates.api.app import (
        _install_shared_runtime_plumbing,
        _shutdown_shared_runtime_plumbing,
    )

    app = _build_app()
    _install_shared_runtime_plumbing(app, _make_settings())
    assert app.state.platform_client is not None
    invalidator_task = app.state.runtime_invalidator_task
    assert invalidator_task is not None

    await _shutdown_shared_runtime_plumbing(app)
    assert app.state.platform_client is None
    assert app.state.runtime_invalidator_task is None
    assert invalidator_task.cancelled() or invalidator_task.done()


@pytest.mark.asyncio
async def test_install_shared_plumbing_wires_file_bundle_cache():
    """shared-mode lifespan constructs a FileBundleCache backed by the
    Hub SDK when ``settings.hub.endpoint`` is configured."""
    from surogates.api.app import (
        _install_shared_runtime_plumbing,
        _shutdown_shared_runtime_plumbing,
    )
    from surogates.runtime import FileBundleCache

    app = _build_app()
    _install_shared_runtime_plumbing(app, _make_settings())
    try:
        assert isinstance(app.state.file_bundle_cache, FileBundleCache)
    finally:
        await _shutdown_shared_runtime_plumbing(app)


def test_install_shared_plumbing_raises_when_hub_disabled():
    """Hub is mandatory in shared mode.  An empty ``hub.endpoint`` is a
    misconfig that must surface at boot, not degrade to filesystem
    reads."""
    from surogates.api.app import _install_shared_runtime_plumbing

    app = _build_app()
    with pytest.raises(RuntimeError, match="hub.endpoint"):
        _install_shared_runtime_plumbing(
            app, _make_settings(hub_endpoint=""),
        )


def test_install_shared_plumbing_raises_when_storage_unconfigured():
    """The memory cache requires a configured storage bucket; an empty
    bucket is a misconfig that raises at boot rather than leaving the
    cache None."""
    from surogates.api.app import _install_shared_runtime_plumbing

    app = _build_app()
    with pytest.raises(RuntimeError, match="storage"):
        _install_shared_runtime_plumbing(
            app, _make_settings(storage_bucket=""),
        )


@pytest.mark.asyncio
async def test_install_shared_plumbing_wires_memory_cache_when_storage_set():
    """When settings.storage.bucket is populated AND a storage
    backend is on app.state, the helper returns a MemoryCache."""
    from surogates.api.app import (
        _install_shared_runtime_plumbing,
        _shutdown_shared_runtime_plumbing,
    )
    from surogates.runtime import MemoryCache

    app = _build_app()
    _install_shared_runtime_plumbing(app, _make_settings())
    try:
        assert isinstance(app.state.memory_cache, MemoryCache)
    finally:
        await _shutdown_shared_runtime_plumbing(app)


@pytest.mark.asyncio
async def test_shutdown_is_safe_when_no_client():
    from surogates.api.app import _shutdown_shared_runtime_plumbing

    app = FastAPI()
    app.state.platform_client = None
    # Must not raise.
    await _shutdown_shared_runtime_plumbing(app)
