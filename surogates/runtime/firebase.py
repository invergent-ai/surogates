"""Per-project Firebase config the shared runtime fetches from
surogate-ops.

Plan 1b / Task 6.  Frozen dataclass with the same shape as the
management-plane's :class:`FirebaseConfigResponse` payload.  The cache
in Task 7 holds these by ``project_id``; the runtime renders the
values into login-page templates.

``enabled_providers`` is a tuple (immutable container for the frozen
context) — projection logic must convert the JSON list to a tuple
exactly like :attr:`AgentRuntimeContext.mcp_server_ids`.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["FirebaseConfig"]


@dataclass(frozen=True, slots=True)
class FirebaseConfig:
    project_id: str
    firebase_project_id: str
    api_key: str
    auth_domain: str
    enabled_providers: tuple[str, ...]
    app_id: str | None = None
    messaging_sender_id: str | None = None
    measurement_id: str | None = None
