"""BOARD_UPDATE events hydrate into user-role messages on replay."""
from __future__ import annotations

from types import SimpleNamespace

from surogates.session.events import EventType


def _event(etype: EventType, data: dict, eid: int):
    return SimpleNamespace(type=etype.value, data=data, id=eid)


def _rebuild(events):
    from surogates.harness.loop_context_replay import ContextReplayMixin

    host = type("_H", (ContextReplayMixin,), {})()
    return host._rebuild_messages(events)


def test_board_event_values():
    assert EventType.BOARD_NOTE.value == "board.note"
    assert EventType.BOARD_UPDATE.value == "board.update"


def test_board_update_event_becomes_user_message():
    content = "[Board update]\n[n3 w1aa/FAIL +2m] dead end"
    events = [
        _event(EventType.USER_MESSAGE, {"content": "hi"}, 1),
        _event(EventType.BOARD_UPDATE, {
            "group_id": "g", "kind": "delta", "cursor_to": 7,
            "content": content,
        }, 2),
    ]
    messages = _rebuild(events)
    assert messages[-1] == {"role": "user", "content": content}


def test_board_update_without_content_is_skipped():
    events = [
        _event(EventType.USER_MESSAGE, {"content": "hi"}, 1),
        _event(EventType.BOARD_UPDATE, {"group_id": "g", "kind": "delta"}, 2),
    ]
    messages = _rebuild(events)
    assert all("Board" not in str(m.get("content")) for m in messages)
