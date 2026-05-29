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

from fastapi import HTTPException, Request

from surogates.runtime.context import AgentRuntimeContext, LLMEndpoint

__all__ = ["agent_runtime_context_dep", "build_agent_runtime_context"]


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


async def _resolve_slug_to_agent_id(
    _request: Request, _slug: str,
) -> str | None:
    """Look up an agent by its DNS-safe slug.

    Stub for Plan 1.  Plan 1b will wire the slug → agent_id mapping
    against the management plane and cache the result.  Returning
    ``None`` here makes the Host-header resolution branch a no-op
    so existing tests + helm-mode callers keep working.
    """
    return None


def _legacy_helm_context(settings, *, agent_id: str) -> AgentRuntimeContext:
    """Build an AgentRuntimeContext from process-wide settings (helm mode).

    The runtime-config endpoint exists only for shared agents, so in
    helm mode we synthesise the context from the values baked into the
    pod's env: ``settings.agent_id`` / ``settings.org_id``.  The other
    fields take harmless defaults — helm-mode code paths never use the
    LLM endpoint accessors here (they keep reading ``settings.llm``
    directly).  Mapped this way so a single dependency works in both
    modes; callers do not branch on runtime_mode at every read site.

    ``project_id`` is left as ``None`` rather than empty-string: helm
    pods do not carry a project id in ``settings``, and downstream
    consumers that need it (Plan 1b Firebase resolution and other
    project-scoped lookups) must detect the absence and fall back —
    silently substituting ``""`` would silently route to project ``""``
    in any DB query that takes the value verbatim.
    """
    return AgentRuntimeContext(
        agent_id=agent_id,
        org_id=getattr(settings, "org_id", "") or "",
        enabled=True,
        config_version=0,
        storage_key_prefix="",
        project_id=None,
    )


async def agent_runtime_context_dep(request: Request) -> AgentRuntimeContext:
    """Resolve the per-request :class:`AgentRuntimeContext`.

    Resolution order (highest precedence first):

    1. ``?agent_id=<id>`` query parameter — explicit, used by Studio
       and admin tools.  Wins even when ``runtime_mode=helm``.
    2. ``Host`` header subdomain (stub in Plan 1).  Plan 1b will
       implement ``slug.example.com`` → ``agent_id`` lookup.
    3. ``request.app.state.settings.agent_id`` fallback when
       ``runtime_mode == "helm"`` — keeps legacy single-agent api
       pods working unchanged.  Intentionally NOT consulted in
       shared mode: a misconfigured shared pod with a stale
       ``settings.agent_id`` would silently route to the wrong
       tenant otherwise.

    Mode-dependent behaviour after agent_id is known:

    * ``helm``: synthesise the context from ``settings`` and return
      it; the cache + management-plane endpoint do not exist in helm
      mode.
    * ``shared``: fetch from the cache (which fronts the management-
      plane endpoint).  Failure responses:

        * ``404`` when surogate-ops refuses the agent (404 from the
          platform = ``runtime_kind != shared`` or row absent).
        * ``503`` when the agent exists but ``enabled == False`` —
          "administratively stopped".  This is the lifecycle gate
          the management plane flips on ``stop_agent``.

    The single shared response in either mode is ``400`` when no
    ``agent_id`` can be resolved at all.
    """
    agent_id = request.query_params.get("agent_id")

    if not agent_id:
        host = request.headers.get("host", "")
        slug = host.split(".", 1)[0] if "." in host else None
        if slug and slug.lower() not in {"www", "api", "localhost"}:
            agent_id = await _resolve_slug_to_agent_id(request, slug)

    settings = getattr(request.app.state, "settings", None)
    runtime_mode = getattr(settings, "runtime_mode", "helm")

    if not agent_id and runtime_mode == "helm":
        agent_id = getattr(settings, "agent_id", "") or None

    if not agent_id:
        raise HTTPException(400, "no agent_id in request")

    if runtime_mode == "helm":
        return _legacy_helm_context(settings, agent_id=agent_id)

    cache = request.app.state.runtime_config_cache
    try:
        payload = await cache.get(agent_id)
    except LookupError:
        raise HTTPException(
            404,
            f"agent {agent_id} not configured for shared runtime",
        )

    ctx = build_agent_runtime_context(payload)
    if not ctx.enabled:
        raise HTTPException(
            503,
            f"agent {agent_id} is stopped (enabled=false)",
        )
    return ctx
