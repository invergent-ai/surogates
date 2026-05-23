"""Expert outcome event emission.

Emits ``EXPERT_RESULT`` or ``EXPERT_FAILURE`` events for each
consultation so the SQL views, training collector, and feedback API
have a complete trajectory. Auto-disable is intentionally not
implemented — operators retire experts manually via
``POST /v1/skills/{name}/retire``. Quality signals come from
``EXPERT_ENDORSE`` / ``EXPERT_OVERRIDE`` events the feedback API
emits when a user or judge rates an ``expert.result``.
"""

from __future__ import annotations

import logging
from typing import Any

from surogates.session.events import EventType

logger = logging.getLogger(__name__)


async def record_expert_outcome(
    *,
    session_store: Any,
    session_id: Any,
    expert_name: str,
    success: bool,
    iterations_used: int = 0,
    error: str | None = None,
    content: str | None = None,
    forced: bool = False,
    category: str | None = None,
) -> None:
    """Emit the outcome event for an expert consultation.

    Parameters
    ----------
    session_store:
        The :class:`~surogates.session.store.SessionStore` for emitting
        events. When ``None`` the function is a no-op.
    session_id:
        The current session UUID.
    expert_name:
        The name of the expert that was consulted.
    success:
        ``True`` if the expert completed without error.
    iterations_used:
        Number of mini-loop iterations the expert consumed.
    error:
        Error message when ``success`` is ``False``.
    content:
        The expert's deliverable text (only present on success).
    forced, category:
        Legacy kwargs preserved for the slash and auto-route paths;
        unused today but retained on the event payload so consumers
        that already key off them keep working.
    """
    if session_store is None:
        return

    event_type = EventType.EXPERT_RESULT if success else EventType.EXPERT_FAILURE
    event_data: dict[str, Any] = {
        "expert": expert_name,
        "success": success,
        "iterations_used": iterations_used,
    }
    if forced:
        event_data["forced"] = True
    if category:
        event_data["category"] = category
    if content is not None:
        event_data["content"] = content
    if error:
        event_data["error"] = error

    try:
        await session_store.emit_event(session_id, event_type, event_data)
    except Exception:
        logger.warning(
            "Failed to emit expert outcome event for %s",
            expert_name,
            exc_info=True,
        )
