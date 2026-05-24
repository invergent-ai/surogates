"""ITERATION_SUMMARY and TURN_SUMMARY event types."""

from __future__ import annotations

from surogates.session.events import EventType


def test_iteration_summary_event_type() -> None:
    assert EventType.ITERATION_SUMMARY.value == "iteration.summary"


def test_turn_summary_event_type() -> None:
    assert EventType.TURN_SUMMARY.value == "turn.summary"


def test_iteration_summary_is_unique() -> None:
    values = [m.value for m in EventType]
    assert values.count("iteration.summary") == 1


def test_turn_summary_is_unique() -> None:
    values = [m.value for m in EventType]
    assert values.count("turn.summary") == 1
