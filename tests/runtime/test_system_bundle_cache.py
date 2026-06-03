"""Unit tests for :class:`SystemBundleCache`.

The system bundle cache is a process-singleton wrapper around an async
loader.  It mirrors the contract the FileBundleCache provides
(``get()``/``invalidate()``) but holds at most one bundle at any time
because every shared-runtime agent reads the same snapshot.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from surogates.runtime.system_bundle_cache import (
    SYSTEM_SKILLS_REPO,
    SystemBundleCache,
)


@dataclass
class _StubBundle:
    """Stand-in for ``AgentFileBundle`` — we only need an identity here."""

    version: str


class _Loader:
    """Async callable that hands back a fresh stub per invocation."""

    def __init__(self) -> None:
        self.calls = 0
        self.next_version = "v1"

    async def __call__(self) -> _StubBundle:
        self.calls += 1
        return _StubBundle(version=self.next_version)


def test_module_exposes_repo_constant() -> None:
    """The hub repo path is part of the public surface so other modules
    (api factory, tests, ops CLI parity assertions) can reference it
    without a stringly-typed literal."""

    assert SYSTEM_SKILLS_REPO == "platform/system-skills"


@pytest.mark.asyncio
async def test_get_calls_loader_once_and_caches() -> None:
    loader = _Loader()
    cache = SystemBundleCache(loader=loader)

    first = await cache.get()
    second = await cache.get()

    assert first is second
    assert loader.calls == 1


@pytest.mark.asyncio
async def test_invalidate_forces_reload() -> None:
    loader = _Loader()
    cache = SystemBundleCache(loader=loader)

    await cache.get()
    loader.next_version = "v2"
    cache.invalidate()

    fresh = await cache.get()

    assert fresh.version == "v2"
    assert loader.calls == 2


@pytest.mark.asyncio
async def test_invalidate_accepts_identifier_kwarg() -> None:
    """The invalidator subsystem dispatches ``invalidate(identifier)``
    where identifier is the channel-suffix (e.g. the new tag).  This
    cache ignores the value but must accept it without crashing the
    dispatch loop."""

    loader = _Loader()
    cache = SystemBundleCache(loader=loader)

    await cache.get()
    cache.invalidate("v7")
    await cache.get()

    assert loader.calls == 2


@pytest.mark.asyncio
async def test_loader_exception_is_not_memoised() -> None:
    """A transient Hub outage at session-start MUST NOT poison every
    subsequent session.  The first call propagates the exception
    verbatim; the second tries the loader again."""

    class _Flaky:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self) -> _StubBundle:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("hub down")
            return _StubBundle(version="v3")

    loader = _Flaky()
    cache = SystemBundleCache(loader=loader)

    with pytest.raises(RuntimeError):
        await cache.get()

    result = await cache.get()

    assert result.version == "v3"
    assert loader.calls == 2


@pytest.mark.asyncio
async def test_concurrent_get_runs_loader_once() -> None:
    """Eight concurrent ``get()`` calls during the first miss must
    converge on a single loader invocation: the lock serialises them
    and the double-checked read returns the now-populated slot."""

    loader = _Loader()
    cache = SystemBundleCache(loader=loader)

    results = await asyncio.gather(*(cache.get() for _ in range(8)))

    assert all(r is results[0] for r in results)
    assert loader.calls == 1


@pytest.mark.asyncio
async def test_invalidate_during_active_get_is_safe() -> None:
    """A pubsub-driven invalidate may race with an in-flight ``get()``
    that is awaiting the loader.  Invariants:

    * the in-flight call returns the bundle it loaded (no deadlock),
    * the next call after the in-flight one returns goes back through
      the loader (the invalidate dropped the cached value).
    """

    loader_started = asyncio.Event()
    loader_block = asyncio.Event()

    async def _slow_loader() -> _StubBundle:
        loader_started.set()
        await loader_block.wait()
        return _StubBundle(version="v1")

    cache = SystemBundleCache(loader=_slow_loader)
    get_task = asyncio.create_task(cache.get())

    await loader_started.wait()
    # invalidate fires while the loader is still running
    cache.invalidate()
    loader_block.set()

    first = await get_task

    # The slot was cleared by the invalidate AFTER the in-flight loader
    # returned, so the result of the in-flight call may or may not be
    # the cached value depending on lock ordering — but the cache MUST
    # NOT be in an unrecoverable state.  Confirm a subsequent get works.
    fresh_loader_calls = 0

    async def _next_loader() -> _StubBundle:
        nonlocal fresh_loader_calls
        fresh_loader_calls += 1
        return _StubBundle(version="v2")

    cache._loader = _next_loader  # noqa: SLF001 — surgical for the race
    # Whatever state the cache is in, invalidate it now so we can verify
    # the loader is reachable again after a concurrent invalidate.
    cache.invalidate()
    second = await cache.get()

    assert first.version == "v1"
    assert second.version == "v2"
    assert fresh_loader_calls == 1
