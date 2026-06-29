import pytest
from surogates.channels.channel_backfill import (
    BackfillLimits, RawMessage, ChannelMeta,
    filter_messages, bound_messages, format_context_block,
)

DAY = 86400.0

def test_filter_drops_bots_subtypes_and_own():
    msgs = [
        {"user": "U_HUMAN", "text": "hello", "ts": "3.0"},
        {"user": "U_BOT", "text": "i am agent", "ts": "2.0"},          # own bot
        {"user": "U_OTHER", "bot_id": "B1", "text": "spam", "ts": "2.5"},  # other bot
        {"user": "U_HUMAN", "subtype": "channel_join", "text": "joined", "ts": "1.0"},
    ]
    kept = filter_messages(msgs, bot_user_id="U_BOT")
    assert [m["text"] for m in kept] == ["hello"]

def test_bound_message_cap_keeps_newest_returns_oldest_first():
    now = 100 * DAY
    raw = [RawMessage(ts=now - i, author="A", text=f"m{i}") for i in range(10)]  # newest-first
    out = bound_messages(raw, BackfillLimits(max_messages=3), now=now)
    assert [m.text for m in out] == ["m2", "m1", "m0"]  # 3 newest, oldest-to-newest

def test_bound_age_cap_drops_old():
    now = 100 * DAY
    raw = [RawMessage(ts=now - 1, author="A", text="fresh"),
           RawMessage(ts=now - 30 * DAY, author="A", text="stale")]
    out = bound_messages(raw, BackfillLimits(max_age_days=7), now=now)
    assert [m.text for m in out] == ["fresh"]

def test_bound_token_cap_binds():
    now = 100 * DAY
    big = "x" * 4000  # ~1000 tokens at chars/4
    raw = [RawMessage(ts=now - i, author="A", text=big) for i in range(20)]
    out = bound_messages(raw, BackfillLimits(max_tokens=2500), now=now)
    assert len(out) == 2  # with ~1009-token messages, exactly 2 fit

def test_format_block_has_header_and_oldest_first():
    now = 100 * DAY
    meta = ChannelMeta(name="eng-infra", topic="infra", purpose="keep prod up")
    msgs = [RawMessage(ts=now - 2, author="Alice", text="first"),
            RawMessage(ts=now - 1, author="Bob", text="second")]
    block = format_context_block(meta, msgs, now=now)
    assert block is not None
    assert "[channel context" in block and "[/channel context]" in block
    assert "#eng-infra" in block and "keep prod up" in block
    assert block.index("Alice: first") < block.index("Bob: second")

def test_format_returns_none_when_empty():
    meta = ChannelMeta(name="x", topic="", purpose="")
    assert format_context_block(meta, [], now=0.0) is None

def test_limits_from_config_merges_over_defaults():
    lim = BackfillLimits.from_config({"max_messages": 50, "max_pages": 3})
    assert lim.max_messages == 50 and lim.max_pages == 3
    assert lim.max_tokens == 8000 and lim.max_age_days == 7  # untouched defaults
