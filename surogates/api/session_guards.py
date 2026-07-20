"""Shared API guards for session mutability and visibility."""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from surogates.session.models import Session


SCHEDULED_RUN_READ_ONLY_DETAIL = "Scheduled run sessions are read-only."


def is_multi_era_web_session(session: Session) -> bool:
    """A top-level web conversation that is NOT the canonical single-session one.

    These are the sessions the "multi session" capability hides while
    off: children (delegation subtrees) and non-web channels are never
    hidden, and the canonical conversation carries the
    ``config.single_session`` stamp.
    """
    return (
        getattr(session, "channel", None) == "web"
        and getattr(session, "parent_id", None) is None
        and (getattr(session, "config", None) or {}).get("single_session")
        is not True
    )


async def require_session_visible(request: Request, session: Session) -> None:
    """404 hidden multi-era web sessions while "multi session" is off.

    The capability's contract is that non-canonical conversations are
    unreachable — deep links included — so every session-scoped read
    surface (events stream, workspace, artifacts, board, files,
    feedback, input answers) funnels through this guard, not just the
    sessions CRUD.  The capability is resolved by the session's OWN
    agent id straight from the runtime-config cache, so the check
    cannot be steered by request parameters; an unresolvable config
    fails open — the primary gate in the sessions routes still applies.
    """
    if not is_multi_era_web_session(session):
        return
    cache = getattr(request.app.state, "runtime_config_cache", None)
    if cache is None:
        return
    try:
        payload = await cache.get(session.agent_id)
    except Exception:
        return
    if bool(payload.get("multi_session", True)):
        return
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Session {session.id} not found.",
    )


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
