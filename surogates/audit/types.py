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

    # Platform copilot writes (a copilot tool performed a side-effecting
    # action on the chat user's behalf; data field carries action +
    # target_id + tool-specific extras).
    POLICY_COPILOT_ACTION = "policy.copilot_action"

    # Plan 4 — per-user memory writes.  Surface for compliance
    # (the user's memory ends up in the LLM's system prompt) and
    # for operator debugging (who changed what, when).
    MEMORY_WRITE = "memory.write"

    # Plan 4 — concurrent writes from /loop + interactive chat
    # racing on the same memory key.  The R2MemoryStore uses
    # last-write-wins; the audit event surfaces the race rate so
    # admins can dashboard noisy tenants.
    MEMORY_CONFLICT = "memory.conflict"

    # Plan 5 — per-agent MCP call.  Fires at the MCPCallSandbox
    # boundary (Task 12) so every tool invocation lands in the
    # audit log with the agent_id that initiated it + the call
    # outcome (success / timeout / RLIMIT exhausted).
    POLICY_MCP_CALL = "policy.mcp_call"
