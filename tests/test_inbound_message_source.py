"""USER_MESSAGE events carry a normalized source.chat_type.

The harness uses source.chat_type ('dm'/'group') to decide sender attribution,
so it must be present and must win over any adapter-native chat-kind value.
"""

from __future__ import annotations

from types import SimpleNamespace

from surogates.channels.inbound import build_message_source


def _msg(**over):
    base = dict(
        source={},
        identifier="C123",
        platform_user_id="U1",
        user_name="Alice",
        thread_key="C123:1700000000.000100",
        ts="1700000000.000100",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_group_message_gets_group_chat_type():
    src = build_message_source(_msg(), platform="slack", chat_type="group")
    assert src["chat_type"] == "group"
    assert src["user_name"] == "Alice"
    assert src["user_id"] == "U1"
    assert src["ts"] == "1700000000.000100"


def test_dm_message_gets_dm_chat_type():
    src = build_message_source(_msg(thread_key=None), platform="slack", chat_type="dm")
    assert src["chat_type"] == "dm"


def test_normalized_chat_type_overrides_adapter_value():
    # Telegram adapter puts a native 'supergroup' kind in msg.source; our
    # normalized 'group' must win.
    src = build_message_source(
        _msg(source={"chat_type": "supergroup"}),
        platform="telegram", chat_type="group",
    )
    assert src["chat_type"] == "group"


def test_adapter_metadata_preserved_alongside():
    src = build_message_source(
        _msg(source={"channel_type": "channel", "custom": "x"}),
        platform="slack", chat_type="group",
    )
    assert src["channel_type"] == "channel"
    assert src["custom"] == "x"
    assert src["chat_type"] == "group"
