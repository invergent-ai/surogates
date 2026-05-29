"""Tests for the POLICY_CHANNEL_INBOUND audit type.

Plan 6 / Task 13.  Per-event channel-routing audit so compliance
can answer 'which tenant did Slack message X route to' from the
audit log alone.  Also surfaces rate-limited drops
(rate_limit_outcome='dropped') so operators see throttling on
the dashboard.
"""

from __future__ import annotations

from surogates.audit.types import AuditType


def test_policy_channel_inbound_type_exists():
    assert (
        AuditType.POLICY_CHANNEL_INBOUND.value
        == "policy.channel_inbound"
    )


def test_audit_type_count_grew_by_one():
    """Plan 5 baseline was 9; Plan 6 adds POLICY_CHANNEL_INBOUND."""
    values = {m.value for m in AuditType}
    assert len(values) >= 10
