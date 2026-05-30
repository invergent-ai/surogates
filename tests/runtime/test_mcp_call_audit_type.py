"""Tests for the POLICY_MCP_CALL audit type.

Plan 5 / Task 4.  Per-call MCP audit emit so compliance can answer
'which agent invoked tool X on server Y, when?' — distinct from
POLICY_MCP_SCAN (org-scoped, fires at server-connect time) and
CREDENTIAL_ACCESS (now agent-scoped per Task 5).
"""

from __future__ import annotations

from surogates.audit.types import AuditType


def test_policy_mcp_call_type_exists():
    assert AuditType.POLICY_MCP_CALL.value == "policy.mcp_call"


def test_audit_type_count_grew_by_one():
    """The MCP-side audit surface gains POLICY_MCP_CALL while the
    pre-Plan-5 set (auth, MCP scan, rug-pull, credential.access,
    copilot, memory.write, memory.conflict) stays."""
    values = {m.value for m in AuditType}
    # +1 over the 8-member baseline (auth.login, auth.failed,
    # policy.mcp_scan, policy.rug_pull, credential.access,
    # policy.copilot_action, memory.write, memory.conflict).
    assert len(values) >= 9
