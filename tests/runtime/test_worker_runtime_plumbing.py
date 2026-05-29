"""Tests for worker-side shared-runtime plumbing.

Plan 2 / Task 1.  The worker must wire a PlatformClient +
RuntimeConfigCache in shared mode exactly like the api's
``_install_shared_runtime_plumbing`` does — the harness_factory will
pull AgentRuntimeContext through this cache per session.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _make_settings(*, runtime_mode: str = "shared",
                   platform_api_url: str = "https://ops.example.com"):
    return SimpleNamespace(
        runtime_mode=runtime_mode,
        platform_api_url=platform_api_url,
        platform_api_token="t",
        api=SimpleNamespace(rate_limit_rpm=300),
    )


@pytest.mark.asyncio
async def test_install_worker_runtime_plumbing_wires_client_and_cache():
    from surogates.orchestrator.worker import (
        _install_worker_runtime_plumbing,
        _shutdown_worker_runtime_plumbing,
    )
    from surogates.runtime import PlatformClient, RuntimeConfigCache

    state = {}
    settings = _make_settings()
    _install_worker_runtime_plumbing(state, settings)
    try:
        assert isinstance(state["platform_client"], PlatformClient)
        assert isinstance(state["runtime_config_cache"], RuntimeConfigCache)
    finally:
        await _shutdown_worker_runtime_plumbing(state)
        assert state["platform_client"] is None
        assert state["runtime_config_cache"] is None


def test_install_worker_runtime_plumbing_helm_mode_skips():
    from surogates.orchestrator.worker import _install_worker_runtime_plumbing

    state = {}
    _install_worker_runtime_plumbing(state, _make_settings(runtime_mode="helm"))
    assert state["platform_client"] is None
    assert state["runtime_config_cache"] is None


def test_install_worker_runtime_plumbing_shared_with_empty_url_skips_loudly(caplog):
    """Misconfigured shared-mode worker (URL empty) must log an error
    and leave the cache None so the first session bootstrap fails
    fast — silently swallowing would route every session through a
    nil cache."""
    import logging
    from surogates.orchestrator.worker import _install_worker_runtime_plumbing

    state = {}
    with caplog.at_level(logging.ERROR):
        _install_worker_runtime_plumbing(
            state, _make_settings(platform_api_url=""),
        )
    assert state["platform_client"] is None
    assert state["runtime_config_cache"] is None
    assert any(
        "SUROGATES_PLATFORM_API_URL is empty" in rec.message
        for rec in caplog.records
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
    """Plan 2 / Task 2.  The worker subscribes to the same Redis
    invalidation channels as the api so admin changes propagate to
    in-flight sessions without restart."""
    from surogates.orchestrator.worker import (
        _install_worker_runtime_plumbing,
        _start_worker_invalidator,
        _shutdown_worker_runtime_plumbing,
        _stop_worker_invalidator,
    )

    state = {"redis": _FakeRedis()}
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
async def test_install_worker_runtime_plumbing_invalidator_no_op_when_cache_absent():
    """Helm-mode workers (no cache) must not start a listener that
    has nothing to invalidate."""
    from surogates.orchestrator.worker import (
        _install_worker_runtime_plumbing,
        _start_worker_invalidator,
    )

    state = {"redis": _FakeRedis()}
    _install_worker_runtime_plumbing(state, _make_settings(runtime_mode="helm"))
    _start_worker_invalidator(state)
    assert state.get("runtime_invalidator_task") is None
