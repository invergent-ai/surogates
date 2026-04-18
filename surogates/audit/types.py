"""Audit event type enumeration.

The ``audit_log`` table's ``type`` column holds one of these string
values.  These are distinct from :class:`surogates.session.events.EventType`
(which is session-scoped) because audit events have no owning session.
"""

from __future__ import annotations

from enum import Enum, unique


@unique
class AuditType(str, Enum):
    """Type of a tenant-scoped audit log entry.

    The string values use the same ``<domain>.<verb>`` convention as
    session events so cross-table audit queries can filter on a single
    namespace if the consumer unions the two tables.
    """

    # Authentication
    AUTH_LOGIN = "auth.login"
    AUTH_FAILED = "auth.failed"

    # MCP tool safety (scan happens at server connect, outside any session)
    POLICY_MCP_SCAN = "policy.mcp_scan"
    POLICY_RUG_PULL = "policy.rug_pull"

    # Credential vault access (at MCP server resolution, outside session)
    CREDENTIAL_ACCESS = "credential.access"
