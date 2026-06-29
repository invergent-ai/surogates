import pytest
from surogates.channels.platforms.slack import SlackPlatform
from surogates.channels.channel_backfill import BackfillLimits

class FakeClient:
    def __init__(self, info, history_pages):
        self._info = info
        self._pages = list(history_pages)
        self.history_calls = 0
    async def conversations_info(self, channel):
        return {"channel": self._info}
    async def conversations_history(self, **kwargs):
        page = self._pages[self.history_calls]
        self.history_calls += 1
        if isinstance(page, BaseException):
            raise page
        return page
    async def users_info(self, user):
        return {"user": {"profile": {"display_name": f"name-{user}"}}}
    async def auth_test(self):
        return {"user_id": "U_BOT"}

def _platform_with(client):
    p = SlackPlatform()
    p._get_client = lambda token: client          # type: ignore
    return p

@pytest.mark.asyncio
async def test_fetch_returns_meta_and_newest_first_messages():
    info = {"name": "eng", "is_im": False, "is_mpim": False, "is_private": False,
            "topic": {"value": "infra"}, "purpose": {"value": "prod"}}
    page = {"messages": [
        {"user": "U1", "text": "newest", "ts": "3.0"},
        {"user": "U2", "text": "older", "ts": "2.0"},
    ], "has_more": False, "response_metadata": {"next_cursor": ""}}
    p = _platform_with(FakeClient(info, [page]))
    out = await p.fetch_channel_context(
        creds={"bot_token": "xoxb"}, channel_id="C1", limits=BackfillLimits())
    assert out is not None
    meta, msgs = out
    assert meta.name == "eng" and meta.purpose == "prod"
    assert [m.text for m in msgs] == ["newest", "older"]      # newest-first preserved
    assert msgs[0].author == "name-U1"                         # author resolved

@pytest.mark.asyncio
async def test_fetch_skips_dm_and_mpim():
    for flag in ("is_im", "is_mpim"):
        info = {"name": "", flag: True}
        p = _platform_with(FakeClient(info, [{"messages": []}]))
        out = await p.fetch_channel_context(
            creds={"bot_token": "x"}, channel_id="D1", limits=BackfillLimits())
        assert out is None

@pytest.mark.asyncio
async def test_fetch_returns_none_on_error():
    class Boom:
        async def conversations_info(self, channel): raise RuntimeError("not_in_channel")
        async def auth_test(self): return {"user_id": "U_BOT"}
    p = _platform_with(Boom())
    out = await p.fetch_channel_context(
        creds={"bot_token": "x"}, channel_id="C1", limits=BackfillLimits())
    assert out is None

@pytest.mark.asyncio
async def test_fetch_respects_page_budget():
    info = {"name": "eng"}
    page = {"messages": [{"user": "U1", "text": "m", "ts": "2.0"}],
            "has_more": True, "response_metadata": {"next_cursor": "CUR"}}
    client = FakeClient(info, [page, page, page])
    p = _platform_with(client)
    await p.fetch_channel_context(
        creds={"bot_token": "x"}, channel_id="C1",
        limits=BackfillLimits(max_pages=2))
    assert client.history_calls == 2  # stopped at page budget despite has_more

@pytest.mark.asyncio
async def test_fetch_keeps_partial_history_when_next_page_rate_limited():
    class RateLimited(Exception):
        response = {"headers": {"Retry-After": "60"}}

    info = {"name": "eng"}
    page = {"messages": [{"user": "U1", "text": "first page", "ts": "2.0"}],
            "has_more": True, "response_metadata": {"next_cursor": "CUR"}}
    client = FakeClient(info, [page, RateLimited("rate_limited")])
    p = _platform_with(client)
    out = await p.fetch_channel_context(
        creds={"bot_token": "x"}, channel_id="C1",
        limits=BackfillLimits(max_pages=3, fetch_time_budget_s=5.0))
    assert out is not None
    _meta, msgs = out
    assert [m.text for m in msgs] == ["first page"]
