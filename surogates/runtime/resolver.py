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

import logging

from fastapi import HTTPException, Request

from surogates.runtime.context import (
    AgentRuntimeContext,
    LLMEndpoint,
    SlashCommandConfig,
)

logger = logging.getLogger(__name__)

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


def multi_session_from_payload(payload: dict) -> bool:
    """The "multi session" capability from a runtime-config payload.

    Absent key = capability on.  Shared with the session-visibility
    guard so the default is encoded exactly once.
    """
    return bool(payload.get("multi_session", True))


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
        llm_tier_basic=_opt_llm(payload.get("llm_tier_basic")),
        llm_tier_pro=_opt_llm(payload.get("llm_tier_pro")),
        mcp_server_ids=tuple(payload.get("mcp_server_ids") or ()),
        repos=tuple(dict(repo) for repo in (payload.get("repos") or ())),
        ssh_targets=tuple(dict(t) for t in (payload.get("ssh_targets") or ())),
        governance=dict(payload.get("governance") or {}),
        slash_commands=_slash_commands(payload.get("slash_commands")),
        brainstorming_gate=bool(payload.get("brainstorming_gate", True)),
        browser_enabled=bool(payload.get("browser_enabled", True)),
        multi_session=multi_session_from_payload(payload),
        linkable_channels=tuple(payload.get("linkable_channels") or ()),
        end_user_token_allowance=payload.get("end_user_token_allowance"),
        # bundle reference.  Empty strings → None
        # (a misconfigured payload that ships "" must not turn into
        # a Hub fetch against an empty ref).
        bundle_hub_ref=payload.get("bundle_hub_ref") or None,
        bundle_version=payload.get("bundle_version") or None,
        commerce_mode=str(payload.get("commerce_mode") or "free"),
        commerce_buy_url=payload.get("commerce_buy_url") or None,
        media_credits_metered=bool(payload.get("media_credits_metered")),
        # ``is None`` (not falsy) so an explicit 0 — metered but free
        # images — survives; only an absent field takes the default.
        media_image_cents=max(0, int(
            4 if payload.get("media_image_cents") is None
            else payload.get("media_image_cents"),
        )),
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
    except Exception:
        # Transient control-plane failure with no cached copy to serve
        # (cold start racing an ops redeploy / a runtime-config version
        # bump). One immediate retry covers the sub-second blip; a
        # second failure is the client's problem to retry — surface a
        # retryable 503, never an anonymous 500.
        logger.warning(
            "runtime-config fetch failed for agent %s — retrying once",
            agent_id,
            exc_info=True,
        )
        try:
            payload = await cache.get(agent_id)
        except LookupError:
            raise HTTPException(
                404,
                f"agent {agent_id} not configured",
            )
        except Exception:
            raise HTTPException(
                503,
                "runtime configuration temporarily unavailable — "
                "retry shortly",
            )

    ctx = build_agent_runtime_context(payload)
    if not ctx.enabled:
        raise HTTPException(
            503,
            f"agent {agent_id} is stopped (enabled=false)",
        )
    return ctx
