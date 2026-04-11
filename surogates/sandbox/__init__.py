"""Sandbox subsystem -- isolated execution environments for agent tools."""

from __future__ import annotations

from surogates.sandbox.base import Resource, Sandbox, SandboxSpec, SandboxStatus
from surogates.sandbox.kubernetes import K8sSandbox
from surogates.sandbox.pool import SandboxPool
from surogates.sandbox.process import ProcessSandbox

__all__ = [
    "K8sSandbox",
    "ProcessSandbox",
    "Resource",
    "Sandbox",
    "SandboxPool",
    "SandboxSpec",
    "SandboxStatus",
]
