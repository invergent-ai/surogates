"""Tests for harness pending-event filtering."""

from types import SimpleNamespace

from surogates.harness.loop import (
    _actionable_pending_events,
    _slash_loop_already_processed,
)
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


def test_slash_loop_idempotency_detects_prior_response():
    """A post-user-message LLM response means the /loop was already handled."""
    events = [
        _event(10, EventType.USER_MESSAGE),
        _event(11, EventType.HARNESS_WAKE),
        _event(12, EventType.LLM_RESPONSE),
        _event(13, EventType.SESSION_TITLE_UPDATED),
        _event(14, EventType.HARNESS_RECOVERED),
        _event(15, EventType.HARNESS_WAKE),
    ]

    assert _slash_loop_already_processed(events) is True


def test_slash_loop_idempotency_allows_fresh_message():
    """A user message with no following response must run /loop normally."""
    events = [
        _event(10, EventType.USER_MESSAGE),
        _event(11, EventType.HARNESS_WAKE),
    ]

    assert _slash_loop_already_processed(events) is False


def test_slash_loop_idempotency_allows_new_message_after_prior_run():
    """A newer user message past the last response is a fresh /loop turn."""
    events = [
        _event(10, EventType.USER_MESSAGE),
        _event(11, EventType.LLM_RESPONSE),
        _event(12, EventType.USER_MESSAGE),  # second /loop invocation
        _event(13, EventType.HARNESS_WAKE),
    ]

    assert _slash_loop_already_processed(events) is False


def test_slash_loop_idempotency_handles_enum_event_type():
    events = [
        _enum_event(10, EventType.USER_MESSAGE),
        _enum_event(11, EventType.LLM_RESPONSE),
    ]

    assert _slash_loop_already_processed(events) is True


def test_slash_loop_idempotency_returns_false_without_user_message():
    events = [_event(10, EventType.LLM_RESPONSE)]

    assert _slash_loop_already_processed(events) is False
