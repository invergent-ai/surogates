"""Shared agent runtime plumbing (Plan 1+).

Multi-tenant building blocks used by the shared surogates api + worker
pool to serve many agents from a single process.  Each component is
independently importable so plans can layer them on without one big
import cycle.
"""

from __future__ import annotations

from surogates.runtime.cache import RuntimeConfigCache
from surogates.runtime.context import AgentRuntimeContext, LLMEndpoint
from surogates.runtime.firebase import FirebaseConfig
from surogates.runtime.firebase_cache import FirebaseConfigCache
from surogates.runtime.invalidator import (
    INVALIDATION_CHANNELS,
    handle_invalidation_message,
    run_invalidator,
)
from surogates.runtime.platform_client import PlatformAuthError, PlatformClient
from surogates.runtime.resolver import (
    agent_runtime_context_dep,
    build_agent_runtime_context,
)
from surogates.runtime.slug_cache import SlugResolverCache

__all__ = [
    "AgentRuntimeContext",
    "FirebaseConfig",
    "FirebaseConfigCache",
    "INVALIDATION_CHANNELS",
    "LLMEndpoint",
    "PlatformAuthError",
    "PlatformClient",
    "RuntimeConfigCache",
    "SlugResolverCache",
    "agent_runtime_context_dep",
    "build_agent_runtime_context",
    "handle_invalidation_message",
    "run_invalidator",
]
