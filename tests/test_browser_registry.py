"""Tests for surogates.browser.registry.BrowserRegistry."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from surogates.browser.registry import BrowserEntry, BrowserRegistry


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, bytes]] = {}

    async def hset(self, name: str, key: str, value: str | bytes) -> int:
        self.hashes.setdefault(name, {})
        existed = key in self.hashes[name]
        self.hashes[name][key] = value.encode() if isinstance(value, str) else value
        return 0 if existed else 1

    async def hget(self, name: str, key: str):
        return self.hashes.get(name, {}).get(key)

    async def hdel(self, name: str, key: str) -> int:
        if name in self.hashes and key in self.hashes[name]:
            del self.hashes[name][key]
            return 1
        return 0

    async def hkeys(self, name: str):
        return list(self.hashes.get(name, {}).keys())


class TestBrowserRegistry:
    async def test_set_and_get(self) -> None:
        reg = BrowserRegistry(FakeRedis())  # type: ignore[arg-type]
        entry = BrowserEntry(
            session_id="sess-1",
            org_id="org-1",
            user_id="user-1",
            rest_url="http://10.0.0.5:30000",
            cdp_url="ws://10.0.0.5:31000",
            live_view_url="ws://10.0.0.5:32000",
            provisioned_at=datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc),
        )
        await reg.set(entry)
        out = await reg.get("sess-1")
        assert out is not None
        assert out.session_id == "sess-1"
        assert out.rest_url == "http://10.0.0.5:30000"
        assert out.org_id == "org-1"

    async def test_get_missing(self) -> None:
        reg = BrowserRegistry(FakeRedis())  # type: ignore[arg-type]
        assert await reg.get("nope") is None

    async def test_delete_idempotent(self) -> None:
        reg = BrowserRegistry(FakeRedis())  # type: ignore[arg-type]
        await reg.delete("nope")
        entry = BrowserEntry(
            session_id="sess-1",
            org_id="o",
            user_id="u",
            rest_url="r",
            cdp_url="c",
            live_view_url="l",
            provisioned_at=datetime.now(timezone.utc),
        )
        await reg.set(entry)
        await reg.delete("sess-1")
        assert await reg.get("sess-1") is None

    async def test_persists_as_json(self) -> None:
        fake = FakeRedis()
        reg = BrowserRegistry(fake)  # type: ignore[arg-type]
        entry = BrowserEntry(
            session_id="sess-1",
            org_id="o",
            user_id="u",
            rest_url="r",
            cdp_url="c",
            live_view_url="l",
            provisioned_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
        )
        await reg.set(entry)
        raw = fake.hashes["surogates:browser:registry"]["sess-1"]
        decoded = json.loads(raw.decode())
        assert decoded["session_id"] == "sess-1"
        assert decoded["provisioned_at"] == "2026-05-10T00:00:00+00:00"
