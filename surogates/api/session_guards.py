"""Shared API guards for session mutability."""

from __future__ import annotations

from fastapi import HTTPException, status

from surogates.session.models import Session


SCHEDULED_RUN_READ_ONLY_DETAIL = "Scheduled run sessions are read-only."


def is_scheduled_run_session(session: Session) -> bool:
    """Return true for scheduler-owned child sessions."""
    return session.channel == "scheduled" or bool(
        (session.config or {}).get("scheduled_session_id")
    )


def require_user_writable_session(session: Session) -> None:
    """Reject user-initiated mutations against scheduler-owned run records."""
    if is_scheduled_run_session(session):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=SCHEDULED_RUN_READ_ONLY_DETAIL,
        )
