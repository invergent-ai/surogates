"""Group messages are attributed to their sender at replay time.

A shared Slack thread carries turns from several people; the model must see who
said what. DMs and legacy events (no source.chat_type) are left unprefixed, and
the prefix is derived only from the durable event payload so replay is stable.
"""

from __future__ import annotations

from surogates.harness.loop_context_replay import build_user_message_dict


def _data(chat_type=None, user_name="Alice", content="hello", **extra):
    source = {"user_name": user_name}
    if chat_type is not None:
        source["chat_type"] = chat_type
    return {"content": content, "source": source, **extra}


def test_group_message_prefixed_with_sender():
    msg = build_user_message_dict(_data(chat_type="group"))
    assert msg == {"role": "user", "content": "Alice: hello"}


def test_dm_message_not_prefixed():
    msg = build_user_message_dict(_data(chat_type="dm"))
    assert msg["content"] == "hello"


def test_legacy_event_without_chat_type_not_prefixed():
    msg = build_user_message_dict(_data(chat_type=None))
    assert msg["content"] == "hello"


def test_group_message_without_user_name_not_prefixed():
    data = {"content": "hello", "source": {"chat_type": "group"}}
    assert build_user_message_dict(data)["content"] == "hello"


def test_group_attribution_is_replay_stable():
    data = _data(chat_type="group")
    assert build_user_message_dict(data) == build_user_message_dict(data)


def test_group_image_message_prefixes_leading_text_block():
    data = _data(
        chat_type="group",
        user_name="Cara",
        content="look",
        images=[{"data": "AAAA", "mime_type": "image/png"}],
    )
    msg = build_user_message_dict(data)
    assert msg["content"][0]["type"] == "text"
    assert msg["content"][0]["text"] == "Cara: look"
