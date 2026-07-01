"""Tests for SlackPlatform.list_channel_files."""
import pytest
from slack_sdk.errors import SlackApiError

from surogates.channels.errors import ChannelApiError
from surogates.channels.platforms.slack import SlackPlatform


class _FakeFilesClient:
    """Minimal AsyncWebClient double exposing files_list."""

    def __init__(self, pages_data=None, *, error_code=None, exc=None):
        # pages_data: list of page payloads, indexed 0-based for page 1..N
        self._pages = pages_data or []
        self._error_code = error_code
        self._exc = exc
        self.calls = []  # records (channel, count, page)

    async def files_list(self, *, channel, count, page):
        self.calls.append((channel, count, page))
        if self._exc is not None:
            raise self._exc
        if self._error_code is not None:
            raise SlackApiError("boom", {"error": self._error_code})
        idx = page - 1
        if idx < len(self._pages):
            return self._pages[idx]
        return {"files": [], "paging": {"pages": len(self._pages), "page": page}}


def _platform_with_client(client):
    p = SlackPlatform()
    p._get_client = lambda token: client  # type: ignore
    return p


# ── two pages aggregated ───────────────────────────────────────────────────

async def test_list_channel_files_aggregates_pages():
    page1 = {"files": [{"id": "F1", "name": "a.txt"}], "paging": {"pages": 2, "page": 1}}
    page2 = {"files": [{"id": "F2", "name": "b.txt"}], "paging": {"pages": 2, "page": 2}}
    client = _FakeFilesClient([page1, page2])
    p = _platform_with_client(client)
    out = await p.list_channel_files(creds={"bot_token": "xoxb"}, channel_id="C1")
    assert [f["id"] for f in out] == ["F1", "F2"]
    # should NOT fetch a page 3
    assert len(client.calls) == 2
    page_nums = [c[2] for c in client.calls]
    assert page_nums == [1, 2]


# ── max_pages cap ──────────────────────────────────────────────────────────

async def test_list_channel_files_bounded_by_max_pages():
    # 5 pages exist but max_pages=2 should stop after 2
    many = [
        {"files": [{"id": f"F{i}"}], "paging": {"pages": 5, "page": i}}
        for i in range(1, 6)
    ]
    client = _FakeFilesClient(many)
    p = _platform_with_client(client)
    out = await p.list_channel_files(creds={"bot_token": "xoxb"}, channel_id="C1", max_pages=2)
    assert len(out) == 2
    assert len(client.calls) == 2


# ── rate-limit ─────────────────────────────────────────────────────────────

async def test_list_channel_files_raises_on_rate_limit():
    client = _FakeFilesClient(error_code="ratelimited")
    p = _platform_with_client(client)
    with pytest.raises(ChannelApiError) as ei:
        await p.list_channel_files(creds={"bot_token": "xoxb"}, channel_id="C1")
    assert ei.value.reason == "rate_limited"


# ── forbidden ─────────────────────────────────────────────────────────────

async def test_list_channel_files_raises_on_forbidden():
    client = _FakeFilesClient(error_code="not_in_channel")
    p = _platform_with_client(client)
    with pytest.raises(ChannelApiError) as ei:
        await p.list_channel_files(creds={"bot_token": "xoxb"}, channel_id="C1")
    assert ei.value.reason == "forbidden"


# ── transient error logs + returns [] ─────────────────────────────────────

async def test_list_channel_files_returns_empty_on_transient_error():
    client = _FakeFilesClient(error_code="some_transient_thing")
    p = _platform_with_client(client)
    out = await p.list_channel_files(creds={"bot_token": "xoxb"}, channel_id="C1")
    assert out == []


# ── missing token/channel → [] ────────────────────────────────────────────

async def test_list_channel_files_empty_without_token():
    p = SlackPlatform()
    out = await p.list_channel_files(creds={}, channel_id="C1")
    assert out == []


async def test_list_channel_files_empty_without_channel():
    p = SlackPlatform()
    out = await p.list_channel_files(creds={"bot_token": "xoxb"}, channel_id="")
    assert out == []
