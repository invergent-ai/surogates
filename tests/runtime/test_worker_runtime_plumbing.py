"""Tests for worker-side shared-runtime plumbing.

The worker wires a PlatformClient +
RuntimeConfigCache + bundle/memory caches exactly like the api's
``_install_shared_runtime_plumbing`` does — the harness_factory will
pull AgentRuntimeContext through this cache per session.

Like the api side, the worker plumbing is fail-loud:
``platform_api_url``, ``hub.endpoint`` and a configured
``storage.bucket`` are mandatory and a misconfig raises at boot.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _make_settings(
    *,
    platform_api_url: str = "https://ops.example.com",
    hub_endpoint: str = "https://hub",
    storage_bucket: str = "test-bucket",
):
    return SimpleNamespace(
        platform_api_url=platform_api_url,
        platform_api_token="t",
        api=SimpleNamespace(rate_limit_rpm=300),
        hub=SimpleNamespace(
            endpoint=hub_endpoint, username="", password="",
        ),
        storage=SimpleNamespace(bucket=storage_bucket, memory_bucket=""),
    )


def _make_state() -> dict:
    """A worker state dict carrying the dependencies the helper reads:
    a redis stub and a storage-backend sentinel (so build_memory_cache
    sees a configured backend)."""
    return {"redis": _FakeRedis(), "storage_backend": object()}


@pytest.mark.asyncio
async def test_install_worker_runtime_plumbing_wires_client_and_cache():
    from surogates.orchestrator.worker import (
        _install_worker_runtime_plumbing,
        _shutdown_worker_runtime_plumbing,
    )
    from surogates.runtime import PlatformClient, RuntimeConfigCache

    state = _make_state()
    _install_worker_runtime_plumbing(state, _make_settings())
    try:
        assert isinstance(state["platform_client"], PlatformClient)
        assert isinstance(state["runtime_config_cache"], RuntimeConfigCache)
    finally:
        await _shutdown_worker_runtime_plumbing(state)
        assert state["platform_client"] is None
        assert state["runtime_config_cache"] is None


def test_install_worker_runtime_plumbing_raises_with_empty_url():
    """Misconfigured shared-mode worker (URL empty) must raise so the
    first session bootstrap never routes through a nil cache —
    silently swallowing would route every session through a nil
    cache."""
    from surogates.orchestrator.worker import _install_worker_runtime_plumbing

    state = _make_state()
    with pytest.raises(RuntimeError, match="SUROGATES_PLATFORM_API_URL"):
        _install_worker_runtime_plumbing(
            state, _make_settings(platform_api_url=""),
        )


class _FakePubsub:
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
async def test_install_worker_runtime_plumbing_starts_invalidator_task():
    """The worker subscribes to the same Redis
    invalidation channels as the api so admin changes propagate to
    in-flight sessions without restart."""
    from surogates.orchestrator.worker import (
        _install_worker_runtime_plumbing,
        _start_worker_invalidator,
        _shutdown_worker_runtime_plumbing,
        _stop_worker_invalidator,
    )

    state = _make_state()
    _install_worker_runtime_plumbing(state, _make_settings())
    _start_worker_invalidator(state)
    try:
        task = state["runtime_invalidator_task"]
        assert task is not None
        assert not task.done()
    finally:
        await _stop_worker_invalidator(state)
        assert state["runtime_invalidator_task"] is None
        await _shutdown_worker_runtime_plumbing(state)


@pytest.mark.asyncio
async def test_install_worker_runtime_plumbing_wires_file_bundle_cache():
    """worker bootstrap mirrors the api lifespan: a FileBundleCache is
    wired when ``settings.hub.endpoint`` is configured."""
    from surogates.orchestrator.worker import (
        _install_worker_runtime_plumbing,
        _shutdown_worker_runtime_plumbing,
    )
    from surogates.runtime import FileBundleCache

    state = _make_state()
    _install_worker_runtime_plumbing(state, _make_settings())
    try:
        assert isinstance(state["file_bundle_cache"], FileBundleCache)
    finally:
        await _shutdown_worker_runtime_plumbing(state)


@pytest.mark.asyncio
async def test_install_worker_runtime_plumbing_wires_memory_cache():
    """worker bootstrap wires a MemoryCache when storage is
    configured."""
    from surogates.orchestrator.worker import (
        _install_worker_runtime_plumbing,
        _shutdown_worker_runtime_plumbing,
    )
    from surogates.runtime import MemoryCache

    state = _make_state()
    _install_worker_runtime_plumbing(state, _make_settings())
    try:
        assert isinstance(state["memory_cache"], MemoryCache)
    finally:
        await _shutdown_worker_runtime_plumbing(state)


def test_start_worker_invalidator_no_op_when_cache_absent():
    """A worker with no runtime_config_cache wired (e.g. before
    bootstrap, or a teardown state) must not start a listener that has
    nothing to invalidate."""
    from surogates.orchestrator.worker import _start_worker_invalidator

    state = {"redis": _FakeRedis()}  # no runtime_config_cache
    _start_worker_invalidator(state)
    assert state.get("runtime_invalidator_task") is None
