"""Resolution helpers for the shared agent runtime (Plan 1, Tasks 14–15).

This module bridges two layers:

1. :func:`build_agent_runtime_context` — pure projection of the
   management-plane JSON payload into an
   :class:`~surogates.runtime.AgentRuntimeContext`.  Stateless, unit
   testable in isolation.  Task 14.
2. :func:`agent_runtime_context_dep` — FastAPI dependency that
   resolves the per-request ``(org_id, agent_id)`` tuple.  Composes
   the projection with the cache + platform client wired on
   ``app.state``.  Task 15.
"""

from __future__ import annotations

from surogates.runtime.context import AgentRuntimeContext, LLMEndpoint

__all__ = ["build_agent_runtime_context"]


def _opt_llm(blob: dict | None) -> LLMEndpoint | None:
    if not blob:
        return None
    return LLMEndpoint(
        model=blob["model"],
        base_url=blob["base_url"],
        api_key_ref=blob["api_key_ref"],
    )


def build_agent_runtime_context(payload: dict) -> AgentRuntimeContext:
    """Project the platform runtime-config payload into a context.

    The projection is intentionally strict for required fields (raises
    ``KeyError`` on missing) and forgiving for optional ones (defaults
    when absent).  Required: ``agent_id``, ``org_id``, ``project_id``,
    ``enabled``, ``version``, ``storage_key_prefix``.  Everything else
    has a defined empty/absent default.

    The optional collections are *copied* (``tuple``, ``dict``) so the
    immutable context cannot be mutated through the caller's payload
    object after construction.
    """
    return AgentRuntimeContext(
        agent_id=payload["agent_id"],
        org_id=payload["org_id"],
        project_id=payload["project_id"],
        enabled=payload["enabled"],
        config_version=payload["version"],
        storage_key_prefix=payload["storage_key_prefix"],
        api_web_url=payload.get("api_web_url"),
        llm_main=_opt_llm(payload.get("llm_main")),
        llm_summary=_opt_llm(payload.get("llm_summary")),
        llm_vision=_opt_llm(payload.get("llm_vision")),
        llm_advisor=_opt_llm(payload.get("llm_advisor")),
        mcp_server_ids=tuple(payload.get("mcp_server_ids") or ()),
        governance=dict(payload.get("governance") or {}),
    )
