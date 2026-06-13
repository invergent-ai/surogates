"""Sandbox protocol and value types.

Defines the abstract interface every sandbox backend must implement,
plus the data classes used to configure and inspect sandboxes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class SandboxUnavailableError(RuntimeError):
    """Raised when the sandbox subsystem itself is broken.

    Distinct from a tool-level failure (bad command, non-zero exit).
    Examples: missing K8s ServiceAccount, image pull failure, pod never
    becomes ready, exec API returns 401/403/404.  When this is raised
    every sandbox-routed tool will fail until the underlying issue is
    resolved, so the harness should surface it to the LLM as a
    "stop trying sandbox tools" signal rather than a per-command error.
    """

    def __init__(self, reason: str, *, classification: str = "infra") -> None:
        super().__init__(reason)
        self.reason = reason
        self.classification = classification


def sandbox_unavailable_result(
    reason: str, *, tools_affected: list[str] | None = None,
) -> str:
    """Build the JSON tool result returned to the LLM when the sandbox is down.

    Uses a fixed ``error: "sandbox_unavailable"`` shape so the model can
    recognise the failure class and stop dispatching every sandbox tool
    in turn (which would all fail identically).
    """
    payload: dict[str, object] = {
        "error": "sandbox_unavailable",
        "reason": reason,
        "guidance": (
            "The sandbox subsystem is unavailable -- every sandbox-routed "
            "tool (terminal, file ops) will fail with the same error "
            "until the underlying infrastructure is fixed.  Do not retry "
            "sandbox tools.  Use harness-local tools (web_search, "
            "web_extract, web_crawl, skills_list, skill_view) or report "
            "the failure to the user."
        ),
    }
    if tools_affected:
        payload["tools_affected"] = tools_affected
    return json.dumps(payload)


class SandboxStatus(str, Enum):
    """Observable lifecycle states for a sandbox instance."""

    RUNNING = "running"
    PENDING = "pending"
    FAILED = "failed"
    TERMINATED = "terminated"


@dataclass(frozen=True, slots=True)
class Resource:
    """A volume or artefact to mount inside the sandbox.

    ``source_ref`` follows a URI scheme:
    - ``pvc://name``        -- Kubernetes PersistentVolumeClaim
    - ``git://repo@branch`` -- Git repository checkout
    - ``emptydir://``       -- Ephemeral scratch space
    """

    source_ref: str
    mount_path: str


@dataclass(slots=True)
class SandboxSpec:
    """Desired-state specification for provisioning a sandbox.

    ``cpu`` / ``memory`` are Kubernetes *requests* (guaranteed minimum).
    ``cpu_limit`` / ``memory_limit`` are Kubernetes *limits* (burst ceiling).
    Separating them lets the sandbox burst above its request when the node
    has spare capacity, while keeping the scheduler honest about placement.
    """

    image: str = "ghcr.io/invergent-ai/surogates-agent-sandbox:latest"
    resources: list[Resource] = field(default_factory=list)
    cpu: str = "2"
    memory: str = "4Gi"
    cpu_limit: str = "4"
    memory_limit: str = "8Gi"
    timeout: int = 300
    env: dict[str, str] = field(default_factory=dict)
    # Root sandbox session key (set by the spec builder). Docker uses it for
    # container labels and stale-container cleanup; K8sSandbox ignores it.
    session_id: str = ""
    # Host-bindable workspace path when one exists. Docker bind-mounts it;
    # K8sSandbox ignores it (its workspace is mounted by the s3fs sidecar).
    workspace_path: str | None = None


def default_sandbox_spec() -> SandboxSpec:
    """Return a :class:`SandboxSpec` seeded from ``SUROGATES_SANDBOX_DEFAULT_*``.

    Worker callsites that have no per-tenant baseline use this so
    deployment-level defaults flow into the pod manifest without
    code changes.  Without env overrides this yields the same
    values as ``SandboxSpec()`` (the two default sets are aligned
    in :class:`surogates.config.SandboxSettings`).
    """
    from surogates.config import SandboxSettings

    s = SandboxSettings()
    return SandboxSpec(
        cpu=s.default_cpu,
        memory=s.default_memory,
        cpu_limit=s.default_cpu_limit,
        memory_limit=s.default_memory_limit,
        timeout=s.default_timeout,
    )


class Sandbox(Protocol):
    """Backend-agnostic sandbox lifecycle protocol.

    Implementations include :class:`~surogates.sandbox.process.ProcessSandbox`
    (Phase 1, subprocess-based) and the forthcoming Kubernetes backend.
    """

    async def provision(self, spec: SandboxSpec) -> str:
        """Create a new sandbox and return its unique identifier."""
        ...

    async def execute(self, sandbox_id: str, name: str, input: str) -> str:
        """Run *name* inside the sandbox with *input*, returning a JSON result."""
        ...

    async def destroy(self, sandbox_id: str) -> None:
        """Tear down the sandbox and release all resources."""
        ...

    async def status(self, sandbox_id: str) -> SandboxStatus:
        """Return the current lifecycle status of the sandbox."""
        ...
