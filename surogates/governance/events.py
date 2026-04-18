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


# ---------------------------------------------------------------------------
# Saga events
# ---------------------------------------------------------------------------


def saga_start_event(saga_id: str, session_id: str) -> dict[str, Any]:
    """Build the data payload for a ``SAGA_START`` event."""
    return {
        "saga_id": saga_id,
        "session_id": session_id,
        "timestamp": time.time(),
    }


def saga_step_event(
    saga_id: str,
    step_id: str,
    tool_name: str,
    state: str,
    *,
    tool_call_id: str = "",
    arguments: dict[str, Any] | None = None,
    compensation_tool: str | None = None,
    compensation_args: dict[str, Any] | None = None,
    checkpoint_hash: str | None = None,
    result: Any | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Build the data payload for a saga step event.

    Used for ``SAGA_STEP_BEGIN``, ``SAGA_STEP_COMMITTED``, and
    ``SAGA_STEP_FAILED`` events.  Fields that are ``None`` are omitted
    from the payload.
    """
    payload: dict[str, Any] = {
        "saga_id": saga_id,
        "step_id": step_id,
        "tool_name": tool_name,
        "state": state,
        "timestamp": time.time(),
    }
    if tool_call_id:
        payload["tool_call_id"] = tool_call_id
    if arguments is not None:
        payload["arguments"] = arguments
    if compensation_tool is not None:
        payload["compensation_tool"] = compensation_tool
    if compensation_args is not None:
        payload["compensation_args"] = compensation_args
    if checkpoint_hash is not None:
        payload["checkpoint_hash"] = checkpoint_hash
    if result is not None:
        payload["result"] = result
    if error is not None:
        payload["error"] = error
    return payload


def saga_compensate_event(
    saga_id: str,
    steps_rolled_back: int,
    reason: str,
    *,
    failed_steps: list[str] | None = None,
) -> dict[str, Any]:
    """Build the data payload for a ``SAGA_COMPENSATE`` event."""
    payload: dict[str, Any] = {
        "saga_id": saga_id,
        "steps_rolled_back": steps_rolled_back,
        "reason": reason,
        "timestamp": time.time(),
    }
    if failed_steps:
        payload["failed_steps"] = failed_steps
    return payload


def saga_complete_event(
    saga_id: str,
    status: str,
    steps_executed: int,
) -> dict[str, Any]:
    """Build the data payload for a ``SAGA_COMPLETE`` event."""
    return {
        "saga_id": saga_id,
        "status": status,
        "steps_executed": steps_executed,
        "timestamp": time.time(),
    }
