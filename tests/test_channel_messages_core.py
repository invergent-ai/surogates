import pytest

from surogates.channels.channel_backfill import (
    BACKFILL_HEADER,
    ChannelMeta,
    RawMessage,
    filter_messages_for_query,
    format_context_block,
    normalize_user,
    parse_since,
)

NOW = 1_720_000_000.0  # fixed reference epoch


def _msg(ts, uid, text="x"):
    return RawMessage(ts=ts, author=f"name-{uid}", text=text, author_id=uid)


def test_normalize_user_strips_mention_and_at():
    assert normalize_user("<@U063>") == "U063"
    assert normalize_user("<@U063|flavius>") == "U063"
    assert normalize_user("@U063") == "U063"
    assert normalize_user("U063") == "U063"
    assert normalize_user(None) == ""
    assert normalize_user("  ") == ""


def test_parse_since_relative_and_iso_and_none():
    assert parse_since(None, now=NOW) is None
    assert parse_since("", now=NOW) is None
    assert parse_since("24h", now=NOW) == NOW - 24 * 3600
    assert parse_since("7d", now=NOW) == NOW - 7 * 86400
    # ISO date -> midnight UTC epoch of that date
    assert parse_since("2024-07-03", now=NOW) == 1_719_964_800.0


def test_parse_since_invalid_raises():
    with pytest.raises(ValueError):
        parse_since("banana", now=NOW)
    with pytest.raises(ValueError):
        parse_since("2024-13-01", now=NOW)


def test_parse_since_zero_window_raises():
    # A '0h'/'0d' window is degenerate (cutoff == now → matches nothing); reject
    # it so the agent gets feedback instead of a silent empty result.
    with pytest.raises(ValueError):
        parse_since("0h", now=NOW)
    with pytest.raises(ValueError):
        parse_since("0d", now=NOW)


def test_filter_by_user_id_and_limit_returns_oldest_first():
    msgs = [  # newest-first, as fetch_channel_context returns
        _msg(NOW - 10, "U1"), _msg(NOW - 20, "U2"),
        _msg(NOW - 30, "U1"), _msg(NOW - 40, "U1"),
    ]
    out = filter_messages_for_query(msgs, since_cutoff=None, user="U1", limit=2)
    assert [m.ts for m in out] == [NOW - 30, NOW - 10]  # newest 2 of U1, oldest-first


def test_filter_by_display_name_case_insensitive():
    # The rendered channel block shows display names, not U-ids, so a name query
    # must match the author's display name (here 'name-U1').
    msgs = [_msg(NOW - 10, "U1"), _msg(NOW - 20, "U2")]
    out = filter_messages_for_query(msgs, since_cutoff=None, user="NAME-u1", limit=50)
    assert [m.author_id for m in out] == ["U1"]


def test_filter_by_since_drops_older():
    msgs = [_msg(NOW - 10, "U1"), _msg(NOW - 100, "U1")]
    out = filter_messages_for_query(
        msgs, since_cutoff=NOW - 50, user="", limit=50)
    assert [m.ts for m in out] == [NOW - 10]


def test_filter_empty_input_returns_empty():
    assert filter_messages_for_query([], since_cutoff=None, user="", limit=50) == []


def test_format_block_uses_custom_header():
    meta = ChannelMeta(name="surogate", topic="", purpose="")
    block = format_context_block(
        meta, [_msg(NOW - 10, "U1", "hello")], now=NOW, header="[channel messages]")
    assert block.startswith("[channel messages]")
    assert "hello" in block


def test_format_block_default_header_is_not_pre_join():
    meta = ChannelMeta(name="surogate", topic="", purpose="")
    block = format_context_block(meta, [_msg(NOW - 10, "U1", "hi")], now=NOW)
    assert block.startswith(BACKFILL_HEADER)
    assert "before the agent joined" not in block


def test_format_block_empty_messages_returns_none():
    meta = ChannelMeta(name="surogate", topic="", purpose="")
    assert format_context_block(meta, [], now=NOW) is None
