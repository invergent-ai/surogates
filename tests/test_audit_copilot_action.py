"""Unit tests for the ``policy.copilot_action`` audit type + event builder.

The surogate-ops platform copilot performs side-effecting actions on
behalf of the chat user (start training, deploy model, etc.). Those
calls emit an audit row through the Surogates harness's audit store,
keyed by the ``POLICY_COPILOT_ACTION`` audit type and a payload built
by :func:`surogates.audit.events.copilot_action_event`.
"""
from __future__ import annotations

from surogates.audit.types import AuditType


def test_audit_type_has_copilot_action() -> None:
    assert AuditType.POLICY_COPILOT_ACTION.value == "policy.copilot_action"
