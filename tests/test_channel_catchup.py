"""Tests for the boot catch-up watermark + replay."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from surogates.channels.channel_catchup import (
    _watermark_from,
    latest_catchup_watermark,
)


class TestWatermarkFrom:
    def test_prefers_exact_ts(self):
        created = datetime(2024, 4, 5, 12, 0, 0, tzinfo=timezone.utc)
        assert _watermark_from("1712345678.123456", created) == "1712345678.123456"

    def test_falls_back_to_created_at(self):
        created = datetime(2024, 4, 5, 12, 0, 0, 500000, tzinfo=timezone.utc)
        assert _watermark_from(None, created) == f"{created.timestamp():.6f}"

    def test_none_when_no_events(self):
        assert _watermark_from(None, None) is None


# --- watermark query (mocked db) ---------------------------------------------


class _FakeResult:
    def __init__(self, value: str | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> str | None:
        return self._value


class _FakeDB:
    def __init__(self, value: str | None) -> None:
        self._value = value
        self.executed = False

    async def __aenter__(self) -> "_FakeDB":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def execute(self, _stmt: Any, _params: dict[str, Any] | None = None) -> _FakeResult:
        self.executed = True
        return _FakeResult(self._value)


def _sf(value: str | None):
    def factory() -> _FakeDB:
        return _FakeDB(value)

    return factory


class TestLatestCatchupWatermark:
    async def test_returns_exact_ts(self):
        wm = await latest_catchup_watermark(
            _sf("1712345678.123456"),
            org_id="org-1", agent_id="agent-1", api_app_id="A1", chat_id="C1",
        )
        assert wm == "1712345678.123456"

    async def test_falls_back_to_created_at(self):
        created = datetime(2024, 4, 5, 12, 0, 0, tzinfo=timezone.utc)
        wm = await latest_catchup_watermark(
            _sf(f"{created.timestamp():.6f}"),
            org_id="org-1", agent_id="agent-1", api_app_id="A1", chat_id="C1",
        )
        assert wm == f"{created.timestamp():.6f}"

    async def test_none_when_never_seen(self):
        wm = await latest_catchup_watermark(
            _sf(None),
            org_id="org-1", agent_id="agent-1", api_app_id="A1", chat_id="C1",
        )
        assert wm is None
