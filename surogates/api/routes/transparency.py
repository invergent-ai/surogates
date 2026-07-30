"""EU AI Act transparency configuration endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request

from surogates.runtime.governance import disclosure_config, disclosure_text
from surogates.runtime.resolver import resolve_agent_id_soft

router = APIRouter()


@router.get("/transparency")
async def transparency_endpoint(request: Request) -> dict:
    """Return the AI-disclosure config for the frontend.

    The frontend fetches this on load to decide whether to show the
    EU AI Act disclosure banner and which text to display.

    Resolution is per-agent first, via
    :func:`surogates.runtime.resolver.resolve_agent_id_soft` (query
    param, then Host-subdomain slug — the web SPA is served from the
    agent's host and fetches with a relative URL, so it gets its
    agent's config with no client change).  An agent with an explicit
    disclosure config wins outright — including an explicit
    "disabled".  Agents without one, and requests that resolve no
    agent, fall back to the deployment-wide setting.

    This endpoint is public (no authentication required) because the
    disclosure must be shown before the user interacts with the system.
    It never errors on unresolvable agents — the disclosure banner must
    not break the chat page — it just falls back.
    """
    cfg = await _agent_disclosure(request)
    if cfg is not None:
        if not cfg["enabled"]:
            return {"enabled": False}
        return {"enabled": True, "level": cfg["level"], "text": cfg["text"]}

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


async def _agent_disclosure(request: Request) -> dict | None:
    """Best-effort per-agent disclosure lookup; ``None`` when unresolved."""
    try:
        agent_id = await resolve_agent_id_soft(request)
    except Exception:  # noqa: BLE001 — slug cache blip: deployment fallback
        return None
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
    return disclosure_config(governance)
