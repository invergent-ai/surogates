"""Expert outcome tracking and auto-demotion.

Records whether expert invocations succeeded or failed, maintains
running statistics, and auto-disables experts whose success rate drops
below a configurable threshold.

Statistics are stored in the ``expert_stats`` JSONB column of the
``skills`` table (for DB-overlay experts) and emitted as session events
(for all experts regardless of storage).
"""

from __future__ import annotations

import logging
from typing import Any

from surogates.session.events import EventType
from surogates.tools.loader import EXPERT_STATUS_RETIRED

logger = logging.getLogger(__name__)

# An expert is auto-disabled when its success rate drops below this
# threshold AND it has been used at least ``MIN_USES_FOR_AUTO_DISABLE``
# times (to avoid disabling on a single early failure).
AUTO_DISABLE_THRESHOLD: float = 0.60
MIN_USES_FOR_AUTO_DISABLE: int = 20


async def record_expert_outcome(
    *,
    session_store: Any,
    session_id: Any,
    expert_name: str,
    success: bool,
    iterations_used: int = 0,
    error: str | None = None,
    db_session: Any | None = None,
    skill_id: Any | None = None,
) -> None:
    """Record the outcome of an expert invocation.

    Parameters
    ----------
    session_store:
        The :class:`~surogates.session.store.SessionStore` for emitting
        events.
    session_id:
        The current session UUID.
    expert_name:
        The name of the expert that was consulted.
    success:
        ``True`` if the expert completed successfully (no error, no
        override by the user).
    iterations_used:
        Number of loop iterations the expert consumed.
    error:
        Error message if the expert failed.
    db_session:
        Optional async SQLAlchemy session for updating the ``skills``
        table stats.  When ``None``, only the event is emitted.
    skill_id:
        The UUID of the skill row in the ``skills`` table.  Required
        when ``db_session`` is provided.
    """
    # Emit the outcome event.
    event_type = EventType.EXPERT_RESULT if success else EventType.EXPERT_FAILURE
    event_data: dict[str, Any] = {
        "expert": expert_name,
        "success": success,
        "iterations_used": iterations_used,
    }
    if error:
        event_data["error"] = error

    if session_store is not None:
        try:
            await session_store.emit_event(session_id, event_type, event_data)
        except Exception:
            logger.warning(
                "Failed to emit expert outcome event for %s",
                expert_name,
                exc_info=True,
            )

    # Update DB stats if a database session is available.
    if db_session is not None and skill_id is not None:
        await _update_db_stats(
            db_session, skill_id, expert_name, success,
        )


async def _update_db_stats(
    db_session: Any,
    skill_id: Any,
    expert_name: str,
    success: bool,
) -> None:
    """Atomically update expert usage statistics in the skills table.

    Auto-disables the expert if its success rate drops below the
    threshold.
    """
    from surogates.db.models import Skill

    try:
        skill = await db_session.get(Skill, skill_id)
        if skill is None:
            logger.warning("Skill %s not found for stats update", skill_id)
            return

        stats = dict(skill.expert_stats) if skill.expert_stats else {}
        total_uses = stats.get("total_uses", 0) + 1
        total_successes = stats.get("total_successes", 0) + (1 if success else 0)
        stats["total_uses"] = total_uses
        stats["total_successes"] = total_successes
        skill.expert_stats = stats

        # Check for auto-disable.
        if total_uses >= MIN_USES_FOR_AUTO_DISABLE:
            success_rate = total_successes / total_uses
            if success_rate < AUTO_DISABLE_THRESHOLD:
                skill.enabled = False
                skill.expert_status = EXPERT_STATUS_RETIRED
                logger.warning(
                    "Expert '%s' auto-disabled: success rate %.1f%% "
                    "(%d/%d) below threshold %.0f%%",
                    expert_name,
                    success_rate * 100,
                    total_successes,
                    total_uses,
                    AUTO_DISABLE_THRESHOLD * 100,
                )

        await db_session.flush()

    except Exception:
        logger.exception(
            "Failed to update expert stats for skill %s", skill_id,
        )
