"""Shared API guards for session mutability and visibility."""

from __future__ import annotations

import time

from fastapi import HTTPException, Request, status

from surogates.session.models import Session


SCHEDULED_RUN_READ_ONLY_DETAIL = "Scheduled run sessions are read-only."

# Bounded-staleness memo of agent_id → multi_session.  The guard sits on
# high-frequency read routes (event polling, workspace tree) and must not
# ride the runtime-config cache's 1-second TTL into an upstream fetch per
# poll.  A capability flip reaching these secondary surfaces within the
# memo TTL is acceptable — the primary create/list/access gates in the
# sessions routes resolve the capability fresh per request.
_MULTI_SESSION_MEMO_TTL = 30.0
_multi_session_memo: dict[str, tuple[float, bool]] = {}


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


async def require_session_visible(
    request: Request,
    session: Session,
    *,
    multi_session: bool | None = None,
) -> None:
    """404 hidden multi-era web sessions while "multi session" is off.

    The capability's contract is that non-canonical conversations are
    unreachable — deep links included — so every session-scoped read
    surface (events stream, workspace, artifacts, board, files,
    feedback, input answers) funnels through this guard, not just the
    sessions CRUD.  Callers holding a resolved ``AgentRuntimeContext``
    pass ``multi_session`` directly; otherwise the capability is
    resolved (memoized) by the session's OWN agent id from the
    runtime-config cache, so the check cannot be steered by request
    parameters.  An unresolvable config fails open — the primary gate
    in the sessions routes still applies.
    """
    if not is_multi_era_web_session(session):
        return
    if multi_session is None:
        multi_session = await _resolve_multi_session(request, session.agent_id)
    if multi_session:
        return
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Session {session.id} not found.",
    )


async def _resolve_multi_session(request: Request, agent_id: str) -> bool:
    now = time.monotonic()
    memoized = _multi_session_memo.get(agent_id)
    if memoized is not None and (now - memoized[0]) < _MULTI_SESSION_MEMO_TTL:
        return memoized[1]
    cache = getattr(request.app.state, "runtime_config_cache", None)
    if cache is None:
        return True
    try:
        payload = await cache.get(agent_id)
    except Exception:
        return True
    from surogates.runtime.resolver import multi_session_from_payload

    value = multi_session_from_payload(payload)
    _multi_session_memo[agent_id] = (now, value)
    return value


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
