"""Tests for the AGENT_CREATED + AGENT_DELETED audit types.

Plan 7 / Task 1.  Per-agent lifecycle audit so compliance can
answer 'when was tenant X provisioned' and 'who decommissioned
tenant Y, when, with what cascade outcomes' from the audit log
alone.
"""

from surogates.audit.types import AuditType


def test_agent_created_type_exists():
    assert AuditType.AGENT_CREATED.value == "agent.created"


def test_agent_deleted_type_exists():
    assert AuditType.AGENT_DELETED.value == "agent.deleted"


def test_audit_type_count_grew_by_two():
    """Plan 6 baseline was 10; Plan 7 adds AGENT_CREATED and
    AGENT_DELETED."""
    values = {m.value for m in AuditType}
    assert len(values) >= 12
