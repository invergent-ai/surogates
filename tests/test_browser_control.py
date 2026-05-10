"""Tests for surogates.browser.control.BrowserControlStore."""

from __future__ import annotations

from surogates.browser.control import AcquireOutcome, BrowserControlStore


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def get(self, key: str):
        return self.values.get(key)

    async def set(
        self,
        key: str,
        value: str | bytes,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool:
        if nx and key in self.values:
            return False
        self.values[key] = value.encode() if isinstance(value, str) else value
        return True

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if key in self.values:
                del self.values[key]
                deleted += 1
        return deleted


class TestAcquire:
    async def test_acquire_when_unheld(self) -> None:
        store = BrowserControlStore(FakeRedis())  # type: ignore[arg-type]
        outcome, entry = await store.acquire("sess-1", "user-A")
        assert outcome == AcquireOutcome.GRANTED
        assert entry.owner_user_id == "user-A"

    async def test_acquire_same_user_refreshes(self) -> None:
        store = BrowserControlStore(FakeRedis())  # type: ignore[arg-type]
        await store.acquire("sess-1", "user-A")
        outcome, entry = await store.acquire("sess-1", "user-A")
        assert outcome == AcquireOutcome.REFRESHED
        assert entry.owner_user_id == "user-A"

    async def test_acquire_different_user_conflicts(self) -> None:
        store = BrowserControlStore(FakeRedis())  # type: ignore[arg-type]
        await store.acquire("sess-1", "user-A")
        outcome, entry = await store.acquire("sess-1", "user-B")
        assert outcome == AcquireOutcome.CONFLICT
        assert entry.owner_user_id == "user-A"


class TestRelease:
    async def test_release_owner(self) -> None:
        store = BrowserControlStore(FakeRedis())  # type: ignore[arg-type]
        await store.acquire("sess-1", "user-A")
        ok = await store.release("sess-1", "user-A")
        assert ok is True
        assert await store.get("sess-1") is None

    async def test_release_non_owner_rejected(self) -> None:
        store = BrowserControlStore(FakeRedis())  # type: ignore[arg-type]
        await store.acquire("sess-1", "user-A")
        ok = await store.release("sess-1", "user-B")
        assert ok is False
        entry = await store.get("sess-1")
        assert entry is not None
        assert entry.owner_user_id == "user-A"

    async def test_release_unheld_is_noop(self) -> None:
        store = BrowserControlStore(FakeRedis())  # type: ignore[arg-type]
        ok = await store.release("sess-9", "user-X")
        assert ok is False


class TestGet:
    async def test_get_missing(self) -> None:
        store = BrowserControlStore(FakeRedis())  # type: ignore[arg-type]
        assert await store.get("nope") is None

    async def test_held_by_returns_user(self) -> None:
        store = BrowserControlStore(FakeRedis())  # type: ignore[arg-type]
        assert await store.held_by("sess-1") is None
        await store.acquire("sess-1", "user-A")
        assert await store.held_by("sess-1") == "user-A"
