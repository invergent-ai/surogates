"""CLI entry tests.

The K8s Deployment runs ``python -m surogates.scheduled.platform_ticker``
which dispatches to :func:`main`.  Tests use the dependency-
injection seams (redis_factory, store_factory, run_one) so no
real Redis or Postgres is needed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


class _FakeRedis:
    def __init__(self) -> None:
        self._values: dict[str, bytes] = {}
        self.closed = False

    async def set(self, key, value, *, nx=False, xx=False, ex=None):
        existing = self._values.get(key)
        if nx and existing is not None:
            return False
        if xx and existing is None:
            return False
        self._values[key] = value
        return True

    async def get(self, key):
        return self._values.get(key)

    async def delete(self, key):
        return 1 if self._values.pop(key, None) else 0

    async def aclose(self):
        self.closed = True


class _FakeStore:
    def __init__(self) -> None:
        self.calls = 0

    async def find_due_across_tenants(self, **_):
        self.calls += 1
        return []


def _make_settings(*, tick_interval=0.01):
    return SimpleNamespace(
        redis=SimpleNamespace(url="redis://stub"),
        db=SimpleNamespace(),
        worker_id="ticker-test",
        scheduled_sessions=SimpleNamespace(
            tick_interval_seconds=tick_interval,
            leader_ttl_seconds=10,
            leader_lock_key="surogates:scheduled_ticker:leader",
        ),
    )


@pytest.mark.asyncio
async def test_main_wires_lock_store_run_one_and_runs():
    """main() builds the lock + store + run_one and runs the
    ticker.  We use the DI seams so no real Redis or DB is
    needed."""
    from surogates.scheduled import platform_ticker as mod

    fake_redis = _FakeRedis()
    fake_store = _FakeStore()
    ran: list = []

    async def redis_factory(url):
        assert url == "redis://stub"
        return fake_redis

    async def store_factory(settings):
        return fake_store

    async def run_one(row):
        ran.append(row)

    settings = _make_settings()

    # Kick off main() and stop it shortly after; we need to
    # access the running PlatformTicker so we monkeypatch the
    # ticker class to capture the instance.
    captured: dict = {}
    real_ticker = mod.PlatformTicker

    class _Capturing(real_ticker):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            captured["ticker"] = self

    mod.PlatformTicker = _Capturing  # type: ignore[misc]
    try:
        task = asyncio.create_task(mod.main(
            settings=settings,
            redis_factory=redis_factory,
            store_factory=store_factory,
            run_one=run_one,
            install_signal_handlers=False,
        ))
        await asyncio.sleep(0.05)
        captured["ticker"].request_stop()
        await task
    finally:
        mod.PlatformTicker = real_ticker  # type: ignore[misc]

    # The lock was acquired (find_due_across_tenants was called
    # at least once -- the only path through the loop that hits
    # the store is via successful lock acquisition).
    assert fake_store.calls >= 1
    # Redis was closed on teardown.
    assert fake_redis.closed is True


@pytest.mark.asyncio
async def test_main_uses_pid_in_holder_id():
    """The leader-lock holder_id is ``{worker_id}-{pid}`` so a
    K8s pod restart gets a fresh identity -- the new pod's
    release won't no-op against the old pod's previously-held
    lock (the identity check would otherwise misfire)."""
    import os

    from surogates.scheduled import platform_ticker as mod

    fake_redis = _FakeRedis()

    async def redis_factory(url):
        return fake_redis

    async def store_factory(settings):
        return _FakeStore()

    async def run_one(row):
        pass

    settings = _make_settings()
    captured: dict = {}
    real_lock = mod.RedisLeaderLock

    class _CapturingLock(real_lock):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured["holder_id"] = self._holder_id

    mod.RedisLeaderLock = _CapturingLock  # type: ignore[misc]
    real_ticker = mod.PlatformTicker

    class _StoppingTicker(real_ticker):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.request_stop()

    mod.PlatformTicker = _StoppingTicker  # type: ignore[misc]
    try:
        await mod.main(
            settings=settings,
            redis_factory=redis_factory,
            store_factory=store_factory,
            run_one=run_one,
            install_signal_handlers=False,
        )
    finally:
        mod.RedisLeaderLock = real_lock  # type: ignore[misc]
        mod.PlatformTicker = real_ticker  # type: ignore[misc]

    assert captured["holder_id"].startswith("ticker-test-")
    assert str(os.getpid()) in captured["holder_id"]
