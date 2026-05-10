"""Phase C browser resolver and event foundation tests."""

from __future__ import annotations

from surogates.session.events import EventType


def test_browser_control_event_types_exist() -> None:
    assert EventType.BROWSER_CONTROL_GRANTED.value == "browser.control_granted"
    assert EventType.BROWSER_CONTROL_RETURNED.value == "browser.control_returned"


def test_existing_browser_events_unchanged() -> None:
    assert EventType.BROWSER_PROVISIONED.value == "browser.provisioned"
    assert EventType.BROWSER_DESTROYED.value == "browser.destroyed"
