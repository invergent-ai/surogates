"""Stale-on-failure policy of RuntimeConfigCache and the resolver's
retry-then-503 handling of transient control-plane failures."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from surogates.runtime.cache import RuntimeConfigCache
from surogates.runtime.resolver import agent_runtime_context_dep


class _Loader:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def __call__(self, agent_id: str) -> dict:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def test_serves_stale_entry_when_refresh_fails():
    cfg = {"agent_id": "a1", "enabled": True}
    loader = _Loader([cfg, RuntimeError("ops redeploying")])
    cache = RuntimeConfigCache(loader, ttl_seconds=0.0, stale_grace_seconds=60)
    assert await cache.get("a1") == cfg
    # TTL 0: the next call must refresh, the refresh fails, the stale
    # copy is served instead of the error.
    assert await cache.get("a1") == cfg
    assert loader.calls == 2


async def test_failure_with_no_cached_copy_propagates():
    loader = _Loader([RuntimeError("cold start blip")])
    cache = RuntimeConfigCache(loader, ttl_seconds=0.0)
    with pytest.raises(RuntimeError):
        await cache.get("a1")


async def test_lookup_error_purges_and_propagates():
    cfg = {"agent_id": "a1", "enabled": True}
    loader = _Loader([cfg, LookupError("gone"), LookupError("gone")])
    cache = RuntimeConfigCache(loader, ttl_seconds=0.0, stale_grace_seconds=60)
    assert await cache.get("a1") == cfg
    # A definitive 404 must never be masked by a stale copy.
    with pytest.raises(LookupError):
        await cache.get("a1")
    with pytest.raises(LookupError):
        await cache.get("a1")


async def test_stale_grace_expires():
    cfg = {"agent_id": "a1", "enabled": True}
    loader = _Loader([cfg, RuntimeError("down"), RuntimeError("down")])
    cache = RuntimeConfigCache(loader, ttl_seconds=0.0, stale_grace_seconds=0.0)
    assert await cache.get("a1") == cfg
    with pytest.raises(RuntimeError):
        await cache.get("a1")


class _Request:
    def __init__(self, cache, agent_id="a1"):
        class _App:
            pass

        class _State:
            pass

        self.app = _App()
        self.app.state = _State()
        self.app.state.runtime_config_cache = cache
        self.query_params = {"agent_id": agent_id}
        self.headers = {}


_ENABLED_PAYLOAD = {
    "agent_id": "a1",
    "org_id": "o1",
    "project_id": "p1",
    "enabled": True,
    "version": 1,
    "storage_key_prefix": "p1/a1",
}


async def test_resolver_retries_once_and_succeeds():
    loader = _Loader([RuntimeError("blip"), _ENABLED_PAYLOAD])
    cache = RuntimeConfigCache(loader, ttl_seconds=0.0)
    ctx = await agent_runtime_context_dep(_Request(cache))
    assert ctx.agent_id == "a1"
    assert loader.calls == 2


async def test_resolver_returns_503_after_second_failure():
    loader = _Loader([RuntimeError("down"), RuntimeError("still down")])
    cache = RuntimeConfigCache(loader, ttl_seconds=0.0)
    with pytest.raises(HTTPException) as exc:
        await agent_runtime_context_dep(_Request(cache))
    assert exc.value.status_code == 503
    assert "temporarily unavailable" in exc.value.detail


async def test_resolver_404_on_lookup_error_during_retry():
    loader = _Loader([RuntimeError("blip"), LookupError("gone")])
    cache = RuntimeConfigCache(loader, ttl_seconds=0.0)
    with pytest.raises(HTTPException) as exc:
        await agent_runtime_context_dep(_Request(cache))
    assert exc.value.status_code == 404


from surogates.runtime.platform_client import PlatformAuthError  # noqa: E402


async def test_auth_error_is_never_stale_served():
    cfg = {"agent_id": "a1", "enabled": True}
    loader = _Loader([cfg, PlatformAuthError("token revoked")])
    cache = RuntimeConfigCache(loader, ttl_seconds=0.0, stale_grace_seconds=60)
    assert await cache.get("a1") == cfg
    with pytest.raises(PlatformAuthError):
        await cache.get("a1")


async def test_resolver_maps_auth_error_to_named_503_without_retry():
    loader = _Loader([PlatformAuthError("token revoked")])
    cache = RuntimeConfigCache(loader, ttl_seconds=0.0)
    with pytest.raises(HTTPException) as exc:
        await agent_runtime_context_dep(_Request(cache))
    assert exc.value.status_code == 503
    assert "authentication misconfigured" in exc.value.detail
    assert loader.calls == 1  # no retry: the condition never self-heals


async def test_failure_backoff_serves_stale_without_reloading():
    cfg = {"agent_id": "a1", "enabled": True}
    loader = _Loader([cfg, RuntimeError("down"), RuntimeError("down")])
    cache = RuntimeConfigCache(
        loader, ttl_seconds=0.0, stale_grace_seconds=60,
        failure_backoff_seconds=30.0,
    )
    assert await cache.get("a1") == cfg
    assert await cache.get("a1") == cfg  # refresh fails -> stale served
    assert loader.calls == 2
    # Within the backoff window the loader is not re-entered at all.
    for _ in range(5):
        assert await cache.get("a1") == cfg
    assert loader.calls == 2
