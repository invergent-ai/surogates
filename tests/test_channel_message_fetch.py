import types

import pytest

from surogates.channels.channel_backfill import ChannelMeta, RawMessage
from surogates.channels.message_fetch import fetch_channel_messages

NOW = 1_720_000_000.0


def _session(cfg):
    return types.SimpleNamespace(config=cfg, org_id="org-1")


def _platform(context_result, *, capture=None):
    descriptor = types.SimpleNamespace(
        vault_refs=lambda identifier: {"bot_token": "bot_token"})

    async def _fetch_channel_context(*, creds, channel_id, limits, include_bots=False):
        if capture is not None:
            capture["limits"] = limits
            capture["include_bots"] = include_bots
        return context_result

    return types.SimpleNamespace(
        descriptor=descriptor, fetch_channel_context=_fetch_channel_context)


class _Vault:
    async def resolve_ref(self, ref, *, org_id):
        return "xoxb-token"


async def test_read_path_requests_bot_messages():
    """The on-demand tool must read bot/app posts (daily reports), so it asks
    fetch_channel_context to include them."""
    capture = {}
    meta = ChannelMeta(name="reports", topic="", purpose="")
    msgs = [RawMessage(ts=NOW - 10, author="bot", text="PostHog Daily", author_id="U1")]
    platform = _platform((meta, msgs), capture=capture)
    await fetch_channel_messages(
        platform=platform, vault=_Vault(),
        session=_session({"channel_identifier": "A1", "slack_channel_id": "C1"}),
        limit=50, since=None, user=None, now=NOW)
    assert capture["include_bots"] is True


async def test_happy_path_filters_and_formats():
    meta = ChannelMeta(name="surogate", topic="", purpose="")
    msgs = [RawMessage(ts=NOW - 10, author="Flavius", text="hi", author_id="U1")]
    platform = _platform((meta, msgs))
    out = await fetch_channel_messages(
        platform=platform, vault=_Vault(),
        session=_session({"channel_identifier": "A1", "slack_channel_id": "C1"}),
        limit=50, since=None, user="<@U1>", now=NOW)
    assert out["count"] == 1
    assert out["channel"] == "surogate"
    assert "hi" in out["messages_block"]
    assert out["note"] is None


async def test_missing_channel_config_returns_note():
    platform = _platform((ChannelMeta("x", "", ""), []))
    out = await fetch_channel_messages(
        platform=platform, vault=_Vault(),
        session=_session({}), limit=50, since=None, user=None, now=NOW)
    assert out["count"] == 0
    assert out["messages_block"] is None
    assert "channel" in out["note"].lower()


async def test_no_bot_token_returns_note():
    class _EmptyVault:
        async def resolve_ref(self, ref, *, org_id):
            return None

    out = await fetch_channel_messages(
        platform=_platform((ChannelMeta("x", "", ""), [])), vault=_EmptyVault(),
        session=_session({"channel_identifier": "A1", "slack_channel_id": "C1"}),
        limit=50, since=None, user=None, now=NOW)
    assert out["count"] == 0
    assert "token" in out["note"].lower()


async def test_context_none_returns_note():
    out = await fetch_channel_messages(
        platform=_platform(None), vault=_Vault(),
        session=_session({"channel_identifier": "A1", "slack_channel_id": "C1"}),
        limit=50, since=None, user=None, now=NOW)
    assert out["count"] == 0
    assert out["note"]


async def test_no_matching_messages_returns_note_with_channel():
    meta = ChannelMeta(name="surogate", topic="", purpose="")
    msgs = [RawMessage(ts=NOW - 10, author="Flavius", text="hi", author_id="U1")]
    out = await fetch_channel_messages(
        platform=_platform((meta, msgs)), vault=_Vault(),
        session=_session({"channel_identifier": "A1", "slack_channel_id": "C1"}),
        limit=50, since=None, user="U2", now=NOW)
    assert out["count"] == 0
    assert out["channel"] == "surogate"
    assert "user" in out["note"].lower()


async def test_invalid_since_raises():
    with pytest.raises(ValueError):
        await fetch_channel_messages(
            platform=_platform((ChannelMeta("x", "", ""), [])), vault=_Vault(),
            session=_session({"channel_identifier": "A1", "slack_channel_id": "C1"}),
            limit=50, since="banana", user=None, now=NOW)


async def test_display_name_query_matches():
    meta = ChannelMeta(name="surogate", topic="", purpose="")
    msgs = [RawMessage(ts=NOW - 10, author="Flavius", text="hi", author_id="U1")]
    out = await fetch_channel_messages(
        platform=_platform((meta, msgs)), vault=_Vault(),
        session=_session({"channel_identifier": "A1", "slack_channel_id": "C1"}),
        limit=50, since=None, user="flavius", now=NOW)
    assert out["count"] == 1
    assert "hi" in out["messages_block"]


async def test_filter_scans_multiple_pages_but_plain_call_scans_one():
    meta = ChannelMeta(name="surogate", topic="", purpose="")
    msgs = [RawMessage(ts=NOW - 10, author="Flavius", text="hi", author_id="U1")]
    cfg = {"channel_identifier": "A1", "slack_channel_id": "C1"}

    cap_user = {}
    await fetch_channel_messages(
        platform=_platform((meta, msgs), capture=cap_user), vault=_Vault(),
        session=_session(cfg), limit=50, since=None, user="U1", now=NOW)
    assert cap_user["limits"].max_pages > 1  # user filter → multi-page scan

    cap_plain = {}
    await fetch_channel_messages(
        platform=_platform((meta, msgs), capture=cap_plain), vault=_Vault(),
        session=_session(cfg), limit=50, since=None, user=None, now=NOW)
    assert cap_plain["limits"].max_pages == 1  # plain call → single page


async def test_zero_limit_defaults_and_does_not_break():
    meta = ChannelMeta(name="surogate", topic="", purpose="")
    msgs = [RawMessage(ts=NOW - 10, author="Flavius", text="hi", author_id="U1")]
    out = await fetch_channel_messages(
        platform=_platform((meta, msgs)), vault=_Vault(),
        session=_session({"channel_identifier": "A1", "slack_channel_id": "C1"}),
        limit=0, since=None, user=None, now=NOW)
    assert out["count"] == 1  # 0 → default 50, one available message returned


async def test_credential_error_returns_graceful_note():
    class _BoomVault:
        async def resolve_ref(self, ref, *, org_id):
            raise RuntimeError("vault down")

    out = await fetch_channel_messages(
        platform=_platform((ChannelMeta("x", "", ""), [])), vault=_BoomVault(),
        session=_session({"channel_identifier": "A1", "slack_channel_id": "C1"}),
        limit=50, since=None, user=None, now=NOW)
    assert out["count"] == 0
    assert out["note"]
