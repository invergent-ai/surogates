"""A registry entry proven unreachable is removed rather than left to mislead.

Nothing prunes ``surogates:browser:registry`` when a browser dies, and the
server does not always emit ``browser.destroyed``. So ``resolver.resolve``
keeps answering with a browser that is gone, and every consumer downstream has
to discover that by timing out: the preview 502s, the shell blanks for its
whole CDP retry window, and the UI advertises a browser nobody can reach.

A request that has just failed to reach the endpoint is proof the entry is
wrong at that moment, which makes deleting it safe and makes the system
self-healing.
"""

from __future__ import annotations

from surogates.browser.resolver import BrowserResolver


class FakeRegistry:
    def __init__(self, entries: dict) -> None:
        self.entries = dict(entries)
        self.deleted: list[str] = []

    async def get(self, session_id: str):
        return self.entries.get(session_id)

    async def delete(self, session_id: str) -> None:
        self.deleted.append(session_id)
        self.entries.pop(session_id, None)


class _Entry:
    def __init__(self, org_id: str = "org-1") -> None:
        self.org_id = org_id
        self.user_id = ""
        self.rest_url = "http://127.0.0.1:30001"
        self.cdp_url = "ws://127.0.0.1:31001"
        self.live_view_url = "ws://127.0.0.1:32001"
        self.browser_id = "b-1"


async def test_forget_unreachable_removes_the_entry() -> None:
    registry = FakeRegistry({"s-1": _Entry()})
    resolver = BrowserResolver(registry=registry, backend=None)

    await resolver.forget_unreachable("s-1")

    assert registry.deleted == ["s-1"]
    assert await registry.get("s-1") is None


async def test_forget_unreachable_is_safe_when_there_is_nothing_to_forget() -> None:
    registry = FakeRegistry({})
    resolver = BrowserResolver(registry=registry, backend=None)

    # Two callers can race to prune the same dead browser; the second must not
    # raise and take a request down with it.
    await resolver.forget_unreachable("s-1")
    await resolver.forget_unreachable("s-1")


async def test_forget_unreachable_touches_only_the_named_session() -> None:
    registry = FakeRegistry({"s-1": _Entry(), "s-2": _Entry()})
    resolver = BrowserResolver(registry=registry, backend=None)

    await resolver.forget_unreachable("s-1")

    assert registry.deleted == ["s-1"]
    assert await registry.get("s-2") is not None


async def test_forget_unreachable_survives_a_failing_registry() -> None:
    class Broken(FakeRegistry):
        async def delete(self, session_id: str) -> None:
            raise RuntimeError("redis is down")

    resolver = BrowserResolver(registry=Broken({"s-1": _Entry()}), backend=None)

    # Pruning is a courtesy on an already-failing path: it must never turn a
    # browser error into a second, louder one.
    await resolver.forget_unreachable("s-1")
