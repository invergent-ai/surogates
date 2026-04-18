"""Audit event payload builders.

Convenience functions that build the ``data`` payloads attached to
tenant-scoped audit log rows.  Keeping the shape in one place means
both emitters and external consumers reference a single contract.

The stable JSONB schemas are documented in
``docs/audit/audit_log.md``.
"""

from __future__ import annotations

import time
from typing import Any


def auth_login_event(
    method: str,
    *,
    provider: str = "database",
    source_ip: str | None = None,
) -> dict[str, Any]:
    """Build the data payload for an ``auth.login`` entry."""
    payload: dict[str, Any] = {
        "method": method,
        "provider": provider,
        "timestamp": time.time(),
    }
    if source_ip is not None:
        payload["source_ip"] = source_ip
    return payload


def auth_failed_event(
    method: str,
    reason: str,
    *,
    provider: str = "database",
    source_ip: str | None = None,
    attempted_email: str | None = None,
) -> dict[str, Any]:
    """Build the data payload for an ``auth.failed`` entry.

    ``attempted_email`` is recorded only when the caller opts in — do
    not include it for brute-force-style flows where the email would
    leak into logs.
    """
    payload: dict[str, Any] = {
        "method": method,
        "provider": provider,
        "reason": reason,
        "timestamp": time.time(),
    }
    if source_ip is not None:
        payload["source_ip"] = source_ip
    if attempted_email is not None:
        payload["attempted_email"] = attempted_email
    return payload


def mcp_scan_event(
    server_name: str,
    tool_name: str,
    *,
    safe: bool,
    threats: list[str],
    severity: str,
) -> dict[str, Any]:
    """Build the data payload for a ``policy.mcp_scan`` entry.

    Emitted once per MCP tool definition encountered during server
    connection.  ``safe=False`` means the tool was excluded from the
    schema set advertised to the agent.
    """
    return {
        "server": server_name,
        "tool": tool_name,
        "safe": safe,
        "threats": threats,
        "severity": severity,
        "timestamp": time.time(),
    }


def rug_pull_event(
    server_name: str,
    tool_name: str,
    *,
    previous_fingerprint: str,
    current_fingerprint: str,
) -> dict[str, Any]:
    """Build the data payload for a ``policy.rug_pull`` entry.

    Emitted when an MCP tool's SHA-256 fingerprint changes between
    server reconnects — indicates the server altered the tool
    definition after initial registration.
    """
    return {
        "server": server_name,
        "tool": tool_name,
        "previous_fingerprint": previous_fingerprint,
        "current_fingerprint": current_fingerprint,
        "timestamp": time.time(),
    }


def credential_access_event(
    credential_name: str,
    *,
    consumer: str,
    scope: str,
    found: bool,
) -> dict[str, Any]:
    """Build the data payload for a ``credential.access`` entry.

    Parameters
    ----------
    credential_name:
        Name of the credential looked up in the vault.
    consumer:
        What needed the credential (e.g. ``"mcp_server:github"``).
    scope:
        ``"user"`` if resolved from the user's personal vault,
        ``"org"`` if resolved from the org-wide vault, ``"missing"``
        when the credential was not found.
    found:
        Whether the credential was successfully retrieved.
    """
    return {
        "credential": credential_name,
        "consumer": consumer,
        "scope": scope,
        "found": found,
        "timestamp": time.time(),
    }
