"""Unit tests for the ``policy.copilot_action`` audit type + event builder.

The surogate-ops platform copilot performs side-effecting actions on
behalf of the chat user (start training, deploy model, etc.). Those
calls emit an audit row through the Surogates harness's audit store,
keyed by the ``POLICY_COPILOT_ACTION`` audit type and a payload built
by :func:`surogates.audit.events.copilot_action_event`.
"""
from __future__ import annotations

from surogates.audit.events import copilot_action_event
from surogates.audit.types import AuditType


def test_audit_type_has_copilot_action() -> None:
    assert AuditType.POLICY_COPILOT_ACTION.value == "policy.copilot_action"


def test_copilot_action_event_shape() -> None:
    payload = copilot_action_event(
        action="start_training",
        target_id="run-123",
        extras={"dataset_id": "ds-1"},
    )
    assert payload == {
        "action": "start_training",
        "target_id": "run-123",
        "dataset_id": "ds-1",
    }


def test_copilot_action_event_no_extras() -> None:
    payload = copilot_action_event(action="delete_skill", target_id="sk-9")
    assert payload == {"action": "delete_skill", "target_id": "sk-9"}


def test_copilot_action_event_extras_cannot_overwrite_reserved_keys() -> None:
    """``action`` / ``target_id`` come from explicit kwargs and must win."""
    payload = copilot_action_event(
        action="delete_skill",
        target_id="sk-9",
        extras={"action": "evil", "target_id": "evil"},
    )
    assert payload["action"] == "delete_skill"
    assert payload["target_id"] == "sk-9"
