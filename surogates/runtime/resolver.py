"""Resolution helpers for the shared agent runtime.

This module bridges two layers:

1. :func:`build_agent_runtime_context` — pure projection of the
   management-plane JSON payload into an
   :class:`~surogates.runtime.AgentRuntimeContext`.  Stateless, unit
   testable in isolation.
2. :func:`agent_runtime_context_dep` — FastAPI dependency that
   resolves the per-request ``(org_id, agent_id)`` tuple.  Composes
   the projection with the cache + platform client wired on
   ``app.state``.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from surogates.runtime.context import (
    AgentRuntimeContext,
    LLMEndpoint,
    SlashCommandConfig,
)

__all__ = ["agent_runtime_context_dep", "build_agent_runtime_context"]


def _opt_llm(blob: dict | None) -> LLMEndpoint | None:
    if not blob:
        return None
    return LLMEndpoint(
        model=blob["model"],
        base_url=blob["base_url"],
        api_key_ref=blob["api_key_ref"],
    )


# Wire keys (snake_case) → canonical command ids (hyphenated).
_SLASH_WIRE_TO_ID: dict[str, str] = {
    "compress": "compress",
    "code": "code",
    "deep_research": "deep-research",
    "auto_research": "auto-research",
    "loop": "loop",
    "mission": "mission",
    "goal": "goal",
}


def _slash_commands(blob: dict | None) -> SlashCommandConfig:
    """Project the wire ``slash_commands`` object into a SlashCommandConfig.

    Absent / falsy → the permissive default (every command enabled) so a
    runtime-config payload that predates this field keeps current
    behavior.  ``clear`` has no per-command flag and is always included.

    This gate is intentionally **fail-OPEN**: a missing or malformed
    ``slash_commands`` blob re-enables every command rather than locking
    the agent out.  That keeps a projection bug or an older management
    plane from silently bricking slash commands — but it also means such
    a bug surfaces as "a disabled command still works", never as "a
    working command broke".  The ops side must therefore always emit the
    object (the route builds it unconditionally from the agent row).
    """
    if not blob:
        return SlashCommandConfig()
    raw = blob.get("commands") or {}
    ids = {cid for wire, cid in _SLASH_WIRE_TO_ID.items() if raw.get(wire)}
    ids.add("clear")
    return SlashCommandConfig(commands=frozenset(ids))


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
        llm_image=_opt_llm(payload.get("llm_image")),
        llm_video=_opt_llm(payload.get("llm_video")),
        mcp_server_ids=tuple(payload.get("mcp_server_ids") or ()),
        governance=dict(payload.get("governance") or {}),
        slash_commands=_slash_commands(payload.get("slash_commands")),
        browser_enabled=bool(payload.get("browser_enabled", True)),
        # bundle reference.  Empty strings → None
        # (a misconfigured payload that ships "" must not turn into
        # a Hub fetch against an empty ref).
        bundle_hub_ref=payload.get("bundle_hub_ref") or None,
        bundle_version=payload.get("bundle_version") or None,
    )


async def _resolve_slug_to_agent_id(
    request: Request, slug: str,
) -> str | None:
    """Look up an agent by its DNS-safe slug.

    Consults ``request.app.state.slug_resolver_cache`` when wired —
    the cache fronts ``PlatformClient.get_agent_id_for_slug`` and
    memoises both hits and misses so the management plane is not
    hit on every Host-header probe.

    Returns ``None`` when the cache is not wired so the Host-header
    branch silently falls through to the next resolution step
    rather than 500-ing on an AttributeError.
    """
    cache = getattr(request.app.state, "slug_resolver_cache", None)
    if cache is None:
        return None
    return await cache.get(slug)


async def agent_runtime_context_dep(request: Request) -> AgentRuntimeContext:
    """Resolve the per-request :class:`AgentRuntimeContext`.

    Resolution order (highest precedence first):

    1. ``?agent_id=<id>`` query parameter — explicit, used by Studio
       and admin tools.
    2. ``Host`` header subdomain (slug → agent_id via the cache).

    Failure responses:

    * ``400`` when no agent_id can be resolved at all.
    * ``404`` when surogate-ops refuses the agent (row absent).
    * ``503`` when the agent exists but ``enabled == False`` —
      "administratively stopped".  This is the lifecycle gate the
      management plane flips on ``stop_agent``.
    """
    agent_id = request.query_params.get("agent_id")

    if not agent_id:
        host = request.headers.get("host", "")
        slug = host.split(".", 1)[0] if "." in host else None
        if slug and slug.lower() not in {"www", "api", "localhost"}:
            agent_id = await _resolve_slug_to_agent_id(request, slug)

    if not agent_id:
        raise HTTPException(400, "no agent_id in request")

    cache = request.app.state.runtime_config_cache
    try:
        payload = await cache.get(agent_id)
    except LookupError:
        raise HTTPException(
            404,
            f"agent {agent_id} not configured",
        )

    ctx = build_agent_runtime_context(payload)
    if not ctx.enabled:
        raise HTTPException(
            503,
            f"agent {agent_id} is stopped (enabled=false)",
        )
    return ctx
