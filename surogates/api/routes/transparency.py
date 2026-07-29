"""EU AI Act transparency configuration endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request

from surogates.runtime.governance import (
    disclosure_text,
    has_transparency_config,
    transparency_config,
)

router = APIRouter()


@router.get("/transparency")
async def transparency_endpoint(request: Request) -> dict:
    """Return the AI-disclosure config for the frontend.

    The frontend fetches this on load to decide whether to show the
    EU AI Act disclosure banner and which text to display.

    Resolution is per-agent first: an explicit ``?agent_id=`` query
    parameter, else the ``Host`` header subdomain (the web SPA is
    served from the agent's host and fetches with a relative URL, so
    it gets its agent's config with no client change).  An agent that
    carries a transparency block wins outright — including an explicit
    "disabled".  Agents without one, and requests that resolve no
    agent, fall back to the deployment-wide setting.

    This endpoint is public (no authentication required) because the
    disclosure must be shown before the user interacts with the system.
    It never errors on unresolvable agents — the disclosure banner must
    not break the chat page — it just falls back.
    """
    agent_governance = await _resolve_agent_governance(request)
    if has_transparency_config(agent_governance):
        cfg = transparency_config(agent_governance)
        if not cfg["enabled"]:
            return {"enabled": False}
        return {
            "enabled": True,
            "level": cfg["level"],
            "text": disclosure_text(cfg["level"]),
        }

    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        return {"enabled": False}
    t = getattr(settings.governance, "transparency", None)
    if t is None or not getattr(t, "enabled", False):
        return {"enabled": False}
    return {
        "enabled": True,
        "level": t.level,
        "text": disclosure_text(t.level),
    }


async def _resolve_agent_governance(request: Request) -> dict | None:
    """Best-effort per-agent governance lookup; ``None`` when unresolved.

    Mirrors the resolution order of
    :func:`surogates.runtime.resolver.agent_runtime_context_dep`
    (query param, then Host-subdomain slug) but never raises: this
    endpoint pre-dates authentication and must degrade to the
    deployment default instead of failing the page load.
    """
    agent_id = request.query_params.get("agent_id")
    if not agent_id:
        host = request.headers.get("host", "")
        slug = host.split(".", 1)[0] if "." in host else None
        if slug and slug.lower() not in {"www", "api", "localhost"}:
            slug_cache = getattr(
                request.app.state, "slug_resolver_cache", None,
            )
            if slug_cache is not None:
                try:
                    agent_id = await slug_cache.get(slug)
                except Exception:  # noqa: BLE001 — fall back to deployment
                    agent_id = None
    if not agent_id:
        return None

    cache = getattr(request.app.state, "runtime_config_cache", None)
    if cache is None:
        return None
    try:
        payload = await cache.get(agent_id)
    except Exception:  # noqa: BLE001 — unknown agent: deployment fallback
        return None
    governance = payload.get("governance") if isinstance(payload, dict) else None
    return governance if isinstance(governance, dict) else None
