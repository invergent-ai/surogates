"""Smoke test for the inbox EventType values."""

from surogates.session.events import EventType


def test_inbox_event_types_exist_with_documented_values():
    assert EventType.INBOX_INPUT_REQUIRED.value == "inbox.input_required"
    assert EventType.INBOX_TASK_COMPLETE.value == "inbox.task_complete"
    assert EventType.INBOX_GOVERNANCE_GATE.value == "inbox.governance_gate"
    assert EventType.INBOX_PROGRESS_CHECKIN.value == "inbox.progress_checkin"
