"""Conversation-privacy boundary token (memory isolation)."""
from __future__ import annotations

from surogates.channels.memory_boundary import MANAGED_CHANNELS, boundary_token


def _tok(platform, channel_id, visibility, chat_type="", fallback="fb"):
    return boundary_token(
        platform=platform, channel_id=channel_id, visibility=visibility,
        source={"chat_type": chat_type}, fallback_id=fallback,
    )


def test_managed_channels():
    assert MANAGED_CHANNELS == frozenset({"slack", "telegram"})


def test_slack_public_shares_one_token():
    assert _tok("slack", "C111", "public") == "public"
    assert _tok("slack", "C222", "public") == "public"  # all public collapse


def test_slack_private_and_dm_are_isolated_per_channel():
    assert _tok("slack", "G111", "private") == "slack:c:G111"
    assert _tok("slack", "G222", "private") == "slack:c:G222"  # distinct
    assert _tok("slack", "D111", "dm") == "slack:d:D111"


def test_telegram_is_never_public():
    assert _tok("telegram", "100", "private", chat_type="group") == "tg:g:100"
    assert _tok("telegram", "200", "private", chat_type="supergroup") == "tg:g:200"
    assert _tok("telegram", "300", "private", chat_type="channel") == "tg:c:300"
    assert _tok("telegram", "400", "dm", chat_type="private") == "tg:d:400"


def test_unknown_and_blank_fail_closed_isolated():
    assert _tok("slack", "", "public", fallback="s1") == "slack:iso:s1"  # blank id never public
    assert _tok("telegram", "", "private", chat_type="group", fallback="s2") == "telegram:iso:s2"
    assert _tok("matrix", "X1", "public", fallback="s3") == "matrix:iso:s3"  # unknown platform
