"""coalesce_user_messages merges queued steer messages into one user turn."""
from __future__ import annotations

from surogates.harness.loop_context_replay import coalesce_user_messages


def test_single_message_returned_unchanged():
    msg = {"role": "user", "content": "hello"}
    assert coalesce_user_messages([msg]) == msg


def test_two_text_messages_join_with_blank_line():
    out = coalesce_user_messages([
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
    ])
    assert out == {"role": "user", "content": "first\n\nsecond"}


def test_multimodal_message_produces_block_list_in_order():
    img_block = {"type": "image_url", "image_url": {"url": "data:x", "detail": "auto"}}
    out = coalesce_user_messages([
        {"role": "user", "content": "look at this"},
        {"role": "user", "content": [{"type": "text", "text": "and this"}, img_block]},
    ])
    assert out["role"] == "user"
    assert out["content"] == [
        {"type": "text", "text": "look at this"},
        {"type": "text", "text": "and this"},
        img_block,
    ]


def test_empty_text_messages_are_skipped_in_join():
    out = coalesce_user_messages([
        {"role": "user", "content": "kept"},
        {"role": "user", "content": ""},
    ])
    assert out == {"role": "user", "content": "kept"}
