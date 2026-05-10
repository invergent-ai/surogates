"""Browser backend protocol and value types."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class BrowserUnavailableError(RuntimeError):
    """Raised when the browser subsystem is unavailable."""

    def __init__(self, reason: str, *, classification: str = "infra") -> None:
        super().__init__(reason)
        self.reason = reason
        self.classification = classification


def browser_unavailable_result(
    reason: str,
    *,
    tools_affected: list[str] | None = None,
) -> str:
    """Return the JSON tool body for infrastructure-level browser failures."""

    payload: dict[str, object] = {
        "error": "browser_unavailable",
        "reason": reason,
        "guidance": (
            "The agent browser is unavailable; every browser_* tool will fail "
            "with the same error until the underlying infrastructure is fixed. "
            "Do not retry browser tools. Use read-only web tools where possible "
            "or report the failure to the user."
        ),
    }
    if tools_affected:
        payload["tools_affected"] = tools_affected
    return json.dumps(payload)


class BrowserStatus(str, Enum):
    """Observable lifecycle states for a browser instance."""

    RUNNING = "running"
    PENDING = "pending"
    FAILED = "failed"
    TERMINATED = "terminated"


@dataclass(frozen=True, slots=True)
class BrowserEndpoint:
    """URLs exposed by a provisioned browser instance."""

    rest_url: str
    cdp_url: str
    live_view_url: str


@dataclass(slots=True)
class BrowserSpec:
    """Desired-state spec for provisioning a browser instance."""

    image: str = "ghcr.io/invergent-ai/surogates-agent-browser:latest"
    cpu: str = "1"
    memory: str = "2Gi"
    cpu_limit: str = "2"
    memory_limit: str = "4Gi"
    pod_ready_timeout: int = 60
    active_deadline_seconds: int = 3600
    timeout: int = 60
    workspace_path: str | None = None
    workspace_source_ref: str | None = None
    env: dict[str, str] = field(default_factory=dict)


class BrowserBackend(Protocol):
    """Backend-agnostic browser lifecycle protocol."""

    async def provision(
        self,
        spec: BrowserSpec,
        *,
        session_id: str,
        org_id: str,
        user_id: str,
    ) -> tuple[str, BrowserEndpoint]:
        """Create a browser instance and return its id and endpoint.

        Kubernetes-backed instances use session/org/user ids as labels.
        Local process instances accept them for protocol parity and ignore them.
        """
        ...

    async def status(self, browser_id: str) -> BrowserStatus:
        """Return the current lifecycle state."""
        ...

    async def destroy(self, browser_id: str) -> None:
        """Tear down the instance and free its resources."""
        ...
