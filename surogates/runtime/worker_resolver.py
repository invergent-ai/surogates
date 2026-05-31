"""Worker-side per-session runtime-context resolution.

The api resolves :class:`AgentRuntimeContext` per request via
``agent_runtime_context_dep``.  The worker has no HTTP request to
read from — it resolves per session, given the dequeued row.
"""

from __future__ import annotations

from typing import Any, Protocol

from surogates.runtime.cache import RuntimeConfigCache
from surogates.runtime.context import AgentRuntimeContext
from surogates.runtime.resolver import build_agent_runtime_context

__all__ = [
    "AgentDisabledError",
    "resolve_runtime_context_for_session",
]


class AgentDisabledError(RuntimeError):
    """Raised when the resolved AgentRuntimeContext has enabled=False.

    The session must not be processed; the worker requeues / fails it
    according to the dispatcher's policy.  Distinct from
    :class:`LookupError` (agent missing entirely) so the dispatcher
    can pick its strategy (back off for disabled, drop for missing).
    """


class _SessionRowLike(Protocol):
    agent_id: str


async def resolve_runtime_context_for_session(
    session: _SessionRowLike,
    *,
    cache: RuntimeConfigCache | None,
    settings: Any,
) -> AgentRuntimeContext:
    """Project a session row into an :class:`AgentRuntimeContext`.

    Pulls the payload from the worker-side cache (which fronts
    ``PlatformClient.get_runtime_config``) and projects via
    :func:`build_agent_runtime_context`.  Raises
    :class:`AgentDisabledError` when the row is administratively
    stopped (``enabled=False``); ``LookupError`` from the loader
    (agent does not exist) propagates verbatim.
    """
    if cache is None:
        raise RuntimeError(
            "worker has no runtime_config_cache wired; "
            "_install_worker_runtime_plumbing must run before the "
            "harness factory is invoked",
        )

    payload = await cache.get(session.agent_id)
    ctx = build_agent_runtime_context(payload)
    if not ctx.enabled:
        raise AgentDisabledError(
            f"agent {ctx.agent_id} is administratively stopped",
        )
    return ctx
