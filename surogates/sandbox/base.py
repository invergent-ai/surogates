"""Sandbox protocol and value types.

Defines the abstract interface every sandbox backend must implement,
plus the data classes used to configure and inspect sandboxes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


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
    """Desired-state specification for provisioning a sandbox."""

    image: str = "ghcr.io/invergent-ai/agent-sandbox:latest"
    resources: list[Resource] = field(default_factory=list)
    cpu: str = "500m"
    memory: str = "512Mi"
    timeout: int = 300
    env: dict[str, str] = field(default_factory=dict)


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
