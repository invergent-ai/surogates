"""Shared agent runtime plumbing.

Multi-tenant building blocks used by the shared surogates api + worker
pool to serve many agents from a single process.  Each component is
independently importable so plans can layer them on without one big
import cycle.
"""

from __future__ import annotations

from surogates.runtime.agent_principal import (
    ServiceAccountPrincipal,
    make_cached_agent_principal_resolver,
)
from surogates.runtime.bundle_accessor import AgentFileBundle
from surogates.runtime.bundle_cache import FileBundleCache
from surogates.runtime.cache import RuntimeConfigCache
from surogates.runtime.channel_routing_cache import ChannelRoutingCache
from surogates.runtime.context import (
    SLASH_COMMAND_IDS,
    AgentRuntimeContext,
    LLMEndpoint,
    SlashCommandConfig,
)
from surogates.runtime.hub_client import HubBundleClient
from surogates.runtime.memory_cache import MemoryCache
from surogates.runtime.firebase import FirebaseConfig
from surogates.runtime.firebase_cache import FirebaseConfigCache
from surogates.runtime.invalidator import (
    INVALIDATION_CHANNELS,
    handle_invalidation_message,
    run_invalidator,
)
from surogates.runtime.leader_lock import RedisLeaderLock
from surogates.runtime.mate_settings_cache import MateSettingsCache, mate_cache_key
from surogates.runtime.platform_client import PlatformAuthError, PlatformClient
from surogates.runtime.rate_limiter import PerTenantRateLimiter, rate_limit_dep
from surogates.runtime.resolver import (
    agent_runtime_context_dep,
    build_agent_runtime_context,
)
from surogates.runtime.slug_cache import SlugResolverCache
from surogates.runtime.system_bundle_cache import (
    SYSTEM_SKILLS_REPO,
    SystemBundleCache,
)
from surogates.runtime.turn_gate import TurnConcurrencyGate, TurnGateBusy
from surogates.runtime.worker_resolver import (
    AgentDisabledError,
    resolve_runtime_context_for_session,
)

__all__ = [
    "AgentDisabledError",
    "AgentFileBundle",
    "AgentRuntimeContext",
    "ServiceAccountPrincipal",
    "make_cached_agent_principal_resolver",
    "ChannelRoutingCache",
    "FileBundleCache",
    "FirebaseConfig",
    "MateSettingsCache",
    "mate_cache_key",
    "FirebaseConfigCache",
    "HubBundleClient",
    "INVALIDATION_CHANNELS",
    "LLMEndpoint",
    "MemoryCache",
    "PerTenantRateLimiter",
    "PlatformAuthError",
    "PlatformClient",
    "RedisLeaderLock",
    "RuntimeConfigCache",
    "SLASH_COMMAND_IDS",
    "SYSTEM_SKILLS_REPO",
    "SlashCommandConfig",
    "SlugResolverCache",
    "SystemBundleCache",
    "TurnConcurrencyGate",
    "TurnGateBusy",
    "agent_runtime_context_dep",
    "build_agent_runtime_context",
    "handle_invalidation_message",
    "rate_limit_dep",
    "resolve_runtime_context_for_session",
    "run_invalidator",
]
