"""Shared agent runtime plumbing (Plan 1+).

Multi-tenant building blocks used by the shared surogates api + worker
pool to serve many agents from a single process.  Each component is
independently importable so plans can layer them on without one big
import cycle.
"""

from __future__ import annotations

from surogates.runtime.context import AgentRuntimeContext, LLMEndpoint

__all__ = ["AgentRuntimeContext", "LLMEndpoint"]
