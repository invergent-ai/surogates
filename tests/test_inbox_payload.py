"""Unit tests for the inbox payload builder."""

from __future__ import annotations

from surogates.session.events import EventType
from surogates.session.inbox_payload import InboxRow, build_inbox_row


def test_input_required_row_includes_tool_call_id_and_questions():
    row = build_inbox_row(
        event_type=EventType.INBOX_INPUT_REQUIRED,
        event_data={
            "tool_call_id": "tc-1",
            "questions": [
                {"prompt": "Which color?"},
            ],
            "context": "Picking a primary color for the brand",
        },
        session_id="00000000-0000-0000-0000-000000000001",
    )

    assert isinstance(row, InboxRow)
    assert row.kind == "input_required"
    assert "Which color?" in row.title
    assert row.payload["tool_call_id"] == "tc-1"
    assert row.action_ref is not None
    assert row.action_ref["type"] == "clarify_response"
    assert row.action_ref["tool_call_id"] == "tc-1"
    assert row.action_ref["endpoint"].endswith("/respond")


def test_task_complete_row_carries_outcome():
    row = build_inbox_row(
        event_type=EventType.INBOX_TASK_COMPLETE,
        event_data={
            "outcome": "success",
            "summary": "All done.",
            "duration_seconds": 42,
            "session_title": "Refactor billing",
        },
        session_id="00000000-0000-0000-0000-000000000001",
    )

    assert row is not None
    assert row.kind == "task_complete"
    assert row.title == "Refactor billing"
    assert row.payload["outcome"] == "success"
    assert row.action_ref is None


def test_governance_gate_row_includes_tool_call_id():
    row = build_inbox_row(
        event_type=EventType.INBOX_GOVERNANCE_GATE,
        event_data={
            "tool_name": "send_email",
            "tool_call_id": "tc-7",
            "arguments_excerpt": "to=ceo@example.com",
            "deny_reason": "External recipient requires explicit approval.",
            "policy_id": "external-comms-v1",
        },
        session_id="00000000-0000-0000-0000-000000000001",
    )

    assert row is not None
    assert row.kind == "governance_gate"
    assert "send_email" in row.title
    assert row.payload["tool_call_id"] == "tc-7"
    assert row.action_ref is not None
    assert row.action_ref["choices"] == ["approve", "reject"]


def test_progress_checkin_row_is_ack_only():
    row = build_inbox_row(
        event_type=EventType.INBOX_PROGRESS_CHECKIN,
        event_data={
            "progress_summary": "Indexed 1,200 files.",
            "iterations": 14,
            "last_tool": "shell_exec",
            "elapsed_seconds": 1830,
        },
        session_id="00000000-0000-0000-0000-000000000001",
    )

    assert row is not None
    assert row.kind == "progress_checkin"
    assert "14" in row.title
    assert row.action_ref is None


def test_unknown_event_type_returns_none():
    row = build_inbox_row(
        event_type=EventType.LLM_RESPONSE,
        event_data={},
        session_id="00000000-0000-0000-0000-000000000001",
    )

    assert row is None
