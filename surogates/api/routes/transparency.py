"""EU AI Act transparency configuration endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/transparency")
async def transparency_config(request: Request) -> dict:
    """Return the transparency config for the frontend.

    The frontend fetches this on load to decide whether to show the
    EU AI Act disclosure banner and which text to display.

    This endpoint is public (no authentication required) because the
    disclosure must be shown before the user interacts with the system.
    """
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        return {"enabled": False}

    t = getattr(settings.governance, "transparency", None)
    if t is None or not getattr(t, "enabled", False):
        return {"enabled": False}

    return {
        "enabled": True,
        "level": t.level,
        "require_confirmation": t.require_confirmation,
        "emotion_recognition": t.emotion_recognition,
    }
