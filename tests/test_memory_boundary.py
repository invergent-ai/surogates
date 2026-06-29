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


# ---------------------------------------------------------------------------
# session_memory_boundary tests
# ---------------------------------------------------------------------------

from types import SimpleNamespace

from surogates.channels.memory_boundary import session_memory_boundary


def _session(channel, config, sid="s1"):
    return SimpleNamespace(channel=channel, config=config, id=sid)


def test_channel_session_uses_persisted_boundary():
    s = _session("slack", {"memory_boundary": "public"})
    assert session_memory_boundary(s) == "public"


def test_older_slack_public_session_collapses_to_public_only_for_c_prefix():
    s = _session(
        "slack",
        {"slack_channel_id": "C111", "channel_session_key": "agent:slack:group:C111"},
        sid="abc",
    )
    assert session_memory_boundary(s) == "public"


def test_older_slack_non_public_or_ambiguous_session_is_isolated():
    private = _session(
        "slack",
        {"slack_channel_id": "G111", "channel_session_key": "agent:slack:group:G111"},
        sid="abc",
    )
    assert session_memory_boundary(private) == "slack:iso:agent:slack:group:G111"

    dm = _session(
        "slack",
        {"slack_channel_id": "D111", "channel_session_key": "agent:slack:dm:D111"},
        sid="def",
    )
    assert session_memory_boundary(dm) == "slack:iso:agent:slack:dm:D111"

    blank = _session("slack", {"slack_channel_id": ""}, sid="ghi")
    assert session_memory_boundary(blank) == "slack:iso:ghi"


def test_older_telegram_session_without_boundary_is_isolated_not_per_user():
    s = _session(
        "telegram",
        {"telegram_channel_id": "-100", "channel_session_key": "agent:telegram:group:-100"},
        sid="abc",
    )
    assert session_memory_boundary(s) == "telegram:iso:agent:telegram:group:-100"


def test_non_channel_session_is_per_user():
    assert session_memory_boundary(_session("web", {"memory_boundary": "x"})) is None
    assert session_memory_boundary(_session("api", {})) is None
    assert session_memory_boundary(_session("ambient", {})) is None
