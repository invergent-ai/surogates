import types

import pytest

from surogates.channels.channel_backfill import ChannelMeta, RawMessage
from surogates.channels.message_fetch import fetch_channel_messages

NOW = 1_720_000_000.0


def _session(cfg):
    return types.SimpleNamespace(config=cfg, org_id="org-1")


def _platform(context_result):
    descriptor = types.SimpleNamespace(
        vault_refs=lambda identifier: {"bot_token": "bot_token"})

    async def _fetch_channel_context(*, creds, channel_id, limits):
        return context_result

    return types.SimpleNamespace(
        descriptor=descriptor, fetch_channel_context=_fetch_channel_context)


class _Vault:
    async def resolve_ref(self, ref, *, org_id):
        return "xoxb-token"


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
