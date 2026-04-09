"""Governance-specific event helpers.

Convenience functions that build the ``data`` payloads attached to
governance-related entries in the session event log.
"""

from __future__ import annotations

import time
from typing import Any


def policy_denied_event(tool_name: str, reason: str) -> dict[str, Any]:
    """Build the data payload for a ``POLICY_DENIED`` event.

    Parameters
    ----------
    tool_name:
        Fully-qualified name of the tool that was denied.
    reason:
        Human-readable explanation (from :class:`PolicyDecision`).

    Returns
    -------
    dict
        Ready to attach as ``event.data`` on the session event log.
    """
    return {
        "tool": tool_name,
        "reason": reason,
        "timestamp": time.time(),
    }


def mcp_scan_event(
    server_name: str,
    tool_name: str,
    *,
    safe: bool,
    threats: list[str],
    severity: str,
) -> dict[str, Any]:
    """Build the data payload for an MCP tool scan event.

    Parameters
    ----------
    server_name:
        Name of the MCP server that advertised the tool.
    tool_name:
        Name of the scanned tool definition.
    safe:
        Whether the tool passed all safety checks.
    threats:
        List of human-readable threat descriptions (empty when safe).
    severity:
        One of ``"info"``, ``"warning"``, ``"critical"``.
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
    tool_name: str,
    *,
    previous_fingerprint: str,
    current_fingerprint: str,
) -> dict[str, Any]:
    """Build the data payload when a tool definition fingerprint changes.

    This indicates a potential *rug-pull* attack where the MCP server alters
    a tool's definition after initial registration.
    """
    return {
        "tool": tool_name,
        "previous_fingerprint": previous_fingerprint,
        "current_fingerprint": current_fingerprint,
        "timestamp": time.time(),
    }
