"""Shared agent runtime plumbing (Plan 1+).

Multi-tenant building blocks used by the shared surogates api + worker
pool to serve many agents from a single process.  Each component is
independently importable so plans can layer them on without one big
import cycle.
"""

from __future__ import annotations

from surogates.runtime.cache import RuntimeConfigCache
from surogates.runtime.context import AgentRuntimeContext, LLMEndpoint
from surogates.runtime.platform_client import PlatformAuthError, PlatformClient

__all__ = [
    "AgentRuntimeContext",
    "LLMEndpoint",
    "PlatformAuthError",
    "PlatformClient",
    "RuntimeConfigCache",
]
