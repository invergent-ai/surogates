"""Tests for harness pending-event filtering."""

from types import SimpleNamespace

from surogates.harness.loop import _actionable_pending_events
from surogates.session.events import EventType


def _event(event_id: int, event_type: EventType):
    return SimpleNamespace(id=event_id, type=event_type.value)


def _enum_event(event_id: int, event_type: EventType):
    return SimpleNamespace(id=event_id, type=event_type)


def test_recovery_control_events_do_not_count_as_pending_work():
    events = [
        _event(10, EventType.LLM_RESPONSE),
        _event(11, EventType.HARNESS_RECOVERED),
        _event(12, EventType.HARNESS_WAKE),
    ]

    assert _actionable_pending_events(events, cursor=10) == []


def test_recovery_control_enum_events_do_not_count_as_pending_work():
    event = _enum_event(11, EventType.HARNESS_RECOVERED)

    assert _actionable_pending_events([event], cursor=10) == []


def test_user_events_after_cursor_remain_pending_work():
    event = _event(11, EventType.USER_MESSAGE)

    assert _actionable_pending_events([event], cursor=10) == [event]
