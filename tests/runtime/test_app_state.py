"""Tests for shared-runtime plumbing on the FastAPI app.state.

Plan 1 / Task 16.  Verifies the lifespan hook constructs / shuts down
the PlatformClient + RuntimeConfigCache exactly when
``runtime_mode='shared'`` and ``platform_api_url`` is configured.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI


def _make_settings(
    *, runtime_mode: str, platform_api_url: str = "https://ops.example.com",
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
        platform_api_token="t",
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
        from surogates.runtime import SlugResolverCache

        assert isinstance(
            app.state.slug_resolver_cache, SlugResolverCache,
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
async def test_shutdown_is_safe_when_no_client():
    from surogates.api.app import _shutdown_shared_runtime_plumbing

    app = FastAPI()
    app.state.platform_client = None
    # Must not raise.
    await _shutdown_shared_runtime_plumbing(app)
