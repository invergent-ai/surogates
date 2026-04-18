"""Tenant-scoped audit log.

The :mod:`surogates.session` event log captures every action inside a
session.  :mod:`surogates.audit` captures the ones that happen outside
any session: authentication, MCP tool safety scans, credential vault
access.  Both feed the same external audit consumers — ``events`` is
session-scoped, ``audit_log`` is tenant-scoped.
"""

from __future__ import annotations

from surogates.audit.events import (
    auth_failed_event,
    auth_login_event,
    credential_access_event,
    mcp_scan_event,
    rug_pull_event,
)
from surogates.audit.request_meta import client_ip
from surogates.audit.store import AuditStore
from surogates.audit.types import AuditType

__all__ = [
    "AuditStore",
    "AuditType",
    "auth_failed_event",
    "auth_login_event",
    "client_ip",
    "credential_access_event",
    "mcp_scan_event",
    "rug_pull_event",
]
