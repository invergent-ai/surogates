"""FastAPI routes for the missions REST surface.

Auth: every route depends on :func:`get_current_tenant` to extract
``org_id`` and the calling principal (``user_id`` or
``service_account_id``) from the bearer token. Mission rows are
filtered to ``(org_id, principal)`` so cross-tenant access returns 404
(not 403) — the same shape as ``sessions.py``.

Two principal shapes are accepted:
* **User principal** (``user_id`` set) — rows match ``user_id``.
* **Service-account principal** (``service_account_id`` set) — rows
  match ``service_account_id``. This covers Work-chat sessions opened
  through ops, which authenticate as per-user SAs.

Anonymous-channel sessions (neither user nor SA) own no missions and
get empty lists / 404 detail responses — consistent with the harness
loop's gate on ``/mission`` for the same principal shape.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from surogates.db.models import (
    Event,
    Mission as MissionRow,
)
from surogates.missions.models import Mission
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext


router = APIRouter(prefix="/missions")


def _session_factory_dep(request: Request) -> async_sessionmaker:
    """Pull the async_sessionmaker from app state.

    Mirrors the pattern other routes use (see api/routes/sessions.py).
    """
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "session_factory not configured on app.state",
        )
    return factory


def _principal_owns(row: MissionRow, tenant: TenantContext) -> bool:
    """Return True iff the mission belongs to the tenant's principal.

    User-principal tenants match rows where ``user_id == tenant.user_id``;
    service-account-principal tenants match rows where
    ``service_account_id == tenant.service_account_id``. Anonymous-channel
    tenants (no user, no SA) match nothing — they cannot own missions.
    """
    if row.org_id != tenant.org_id:
        return False
    if tenant.user_id is not None:
        return row.user_id == tenant.user_id
    if tenant.service_account_id is not None:
        return row.service_account_id == tenant.service_account_id
    return False


def _principal_where_clause(tenant: TenantContext) -> ColumnElement[bool] | None:
    """Return a SQLAlchemy predicate matching missions owned by the tenant.

    ``None`` when the tenant has no owning principal (channel session) —
    callers should short-circuit to an empty result rather than emit a
    query that would unconditionally match by the ``org_id`` filter.
    """
    if tenant.user_id is not None:
        return MissionRow.user_id == tenant.user_id
    if tenant.service_account_id is not None:
        return MissionRow.service_account_id == tenant.service_account_id
    return None


async def _load_mission_authorized(
    mission_id: UUID,
    *,
    session_factory: async_sessionmaker,
    tenant: TenantContext,
) -> MissionRow:
    """Fetch a mission row and authorize against the request's tenant."""
    async with session_factory() as db:
        row = await db.get(MissionRow, mission_id)
        if row is None or not _principal_owns(row, tenant):
            # 404 (not 403) so cross-tenant probes can't confirm existence.
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"mission {mission_id} not found",
            )
    return row


@router.get("")
async def list_missions(
    status_filter: str = Query("", alias="status"),
    agent_id: str = Query(""),
    session_factory: async_sessionmaker = Depends(_session_factory_dep),
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict[str, Any]:
    """List the caller's missions, newest first.

    ``status`` is a comma-separated allowlist (e.g. ``active,paused``).
    """
    principal_filter = _principal_where_clause(tenant)
    if principal_filter is None:
        # Anonymous-channel tenant — no owned missions possible.
        return {"missions": []}
    statuses = [s.strip() for s in status_filter.split(",") if s.strip()]
    async with session_factory() as db:
        stmt = (
            select(MissionRow)
            .where(
                MissionRow.org_id == tenant.org_id,
                principal_filter,
            )
            .order_by(MissionRow.created_at.desc())
            .limit(100)
        )
        if statuses:
            stmt = stmt.where(MissionRow.status.in_(statuses))
        if agent_id:
            stmt = stmt.where(MissionRow.agent_id == agent_id)
        rows = (await db.execute(stmt)).scalars().all()
    return {
        "missions": [
            Mission.model_validate(r).model_dump(mode="json") for r in rows
        ],
    }


@router.get("/{mission_id}")
async def get_mission(
    mission_id: UUID,
    session_factory: async_sessionmaker = Depends(_session_factory_dep),
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict[str, Any]:
    row = await _load_mission_authorized(
        mission_id, session_factory=session_factory, tenant=tenant,
    )
    return Mission.model_validate(row).model_dump(mode="json")


@router.get("/{mission_id}/workers")
async def get_mission_workers(
    mission_id: UUID,
    session_factory: async_sessionmaker = Depends(_session_factory_dep),
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict[str, Any]:
    """The mission's agents: the sessions its coordinator started with ``agent``
    while the mission was active, oldest first, each with its latest event.

    The client derives a human-friendly activity label from the
    ``latest_event_*`` fields; the server's job is just to expose them.
    """
    from surogates.missions.store import MissionStore

    await _load_mission_authorized(
        mission_id, session_factory=session_factory, tenant=tenant,
    )
    agents = await MissionStore(session_factory).agents(mission_id)
    async with session_factory() as db:
        workers = [await _worker_row(db, agent) for agent in agents]
    return {"workers": workers}


async def _worker_row(db, agent: Any) -> dict[str, Any]:
    """One agent's row, with the session's latest event."""
    latest = (await db.execute(
        select(Event)
        .where(Event.session_id == agent.session_id)
        .order_by(Event.id.desc())
        .limit(1)
    )).scalar_one_or_none()
    return {
        "worker_session_id": str(agent.session_id),
        "agent_def_name": agent.agent_type,
        "description": agent.description,
        "session_status": agent.status,
        "latest_event_id": latest.id if latest else None,
        "latest_event_kind": latest.type if latest else None,
        "latest_event_at": (
            latest.created_at.isoformat()
            if latest and latest.created_at else None
        ),
        "latest_event_summary": (
            json.dumps(latest.data)[:200]
            if latest and latest.data else None
        ),
        "transcript_url": f"/chat/{agent.session_id}",
    }


# Streaming-only chunk types excluded from the mission feed — mirrors
# SurogatesClient._HIDDEN_EVENT_TYPES on the ops side.
_HIDDEN_EVENT_TYPES: frozenset[str] = frozenset({"llm.delta"})


@router.get("/{mission_id}/events")
async def get_mission_events(
    mission_id: UUID,
    after_id: int | None = Query(None, ge=0),
    limit: int = Query(500, ge=1, le=2000),
    session_factory: async_sessionmaker = Depends(_session_factory_dep),
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict[str, Any]:
    """Mission-wide event timeline, ascending by event id.

    Merges the events of the coordinator session (``mission.session_id``)
    and of the mission's agents.  Cursorable by ``after_id`` (bigserial
    event PK). The ``sessions`` map labels every contributing session
    (``coordinator`` or ``agent``) so clients can attribute rows without
    N+1 lookups.
    """
    from surogates.missions.store import MissionStore

    mission_row = await _load_mission_authorized(
        mission_id, session_factory=session_factory, tenant=tenant,
    )
    agents = await MissionStore(session_factory).agents(mission_id)
    session_meta: dict[str, dict[str, Any]] = {
        str(mission_row.session_id): {"agent_def_name": None, "kind": "coordinator"},
    }
    session_ids: list[UUID] = [mission_row.session_id]
    for agent in agents:
        session_meta[str(agent.session_id)] = {"agent_def_name": agent.agent_type, "kind": "agent"}
        session_ids.append(agent.session_id)
    async with session_factory() as db:
        filters = [
            Event.session_id.in_(session_ids),
            Event.type.notin_(_HIDDEN_EVENT_TYPES),
        ]
        if after_id is not None:
            filters.append(Event.id > after_id)
        rows = (await db.execute(
            select(
                Event.id, Event.session_id, Event.type,
                Event.data, Event.created_at,
            )
            .where(*filters)
            .order_by(Event.id.asc())
            .limit(limit)
        )).all()

    return {
        "events": [
            {
                "id": r.id,
                "session_id": str(r.session_id),
                "type": r.type,
                "data": r.data,
                "created_at": (
                    r.created_at.isoformat() if r.created_at else None
                ),
            }
            for r in rows
        ],
        "sessions": session_meta,
    }


# ---------------------------------------------------------------------------
# Mutating routes
# ---------------------------------------------------------------------------


class _PauseBody(BaseModel):
    reason: str | None = None


class _CancelBody(BaseModel):
    reason: str | None = None
    cascade_to_workers: bool = False


def _redis_dep(request: Request) -> Any:
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "redis client not configured on app.state",
        )
    return redis


def _session_store_dep(request: Request) -> Any:
    store = getattr(request.app.state, "session_store", None)
    if store is None:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "session_store not configured on app.state",
        )
    return store


@router.post("/{mission_id}/pause")
async def pause_mission_endpoint(
    mission_id: UUID,
    body: _PauseBody,
    session_factory: async_sessionmaker = Depends(_session_factory_dep),
    session_store: Any = Depends(_session_store_dep),
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict[str, Any]:
    row = await _load_mission_authorized(
        mission_id, session_factory=session_factory, tenant=tenant,
    )
    from surogates.missions.commands import handle_mission_pause
    from surogates.missions.store import MissionStore

    result = await handle_mission_pause(
        session_id=row.session_id, reason=body.reason,
        session_store=session_store,
        mission_store=MissionStore(session_factory),
    )
    if not result.ok:
        raise HTTPException(status.HTTP_409_CONFLICT, result.error)
    return {
        "ok": True, "mission_id": str(result.mission_id), "status": "paused",
    }


@router.post("/{mission_id}/resume")
async def resume_mission_endpoint(
    mission_id: UUID,
    session_factory: async_sessionmaker = Depends(_session_factory_dep),
    session_store: Any = Depends(_session_store_dep),
    redis: Any = Depends(_redis_dep),
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict[str, Any]:
    row = await _load_mission_authorized(
        mission_id, session_factory=session_factory, tenant=tenant,
    )
    from surogates.missions.commands import handle_mission_resume
    from surogates.missions.store import MissionStore

    result = await handle_mission_resume(
        session_id=row.session_id,
        org_id=str(tenant.org_id),
        agent_id=row.agent_id,
        session_store=session_store,
        mission_store=MissionStore(session_factory),
        redis=redis,
    )
    if not result.ok:
        raise HTTPException(status.HTTP_409_CONFLICT, result.error)
    return {
        "ok": True, "mission_id": str(result.mission_id), "status": "active",
    }


@router.post("/{mission_id}/cancel")
async def cancel_mission_endpoint(
    mission_id: UUID,
    body: _CancelBody,
    session_factory: async_sessionmaker = Depends(_session_factory_dep),
    session_store: Any = Depends(_session_store_dep),
    redis: Any = Depends(_redis_dep),
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict[str, Any]:
    row = await _load_mission_authorized(
        mission_id, session_factory=session_factory, tenant=tenant,
    )
    from surogates.missions.commands import handle_mission_cancel
    from surogates.missions.store import MissionStore

    result = await handle_mission_cancel(
        session_id=row.session_id, reason=body.reason,
        cascade_to_workers=body.cascade_to_workers,
        session_store=session_store, session_factory=session_factory,
        mission_store=MissionStore(session_factory),
        redis=redis,
    )
    if not result.ok:
        raise HTTPException(status.HTTP_409_CONFLICT, result.error)
    return {
        "ok": True, "mission_id": str(result.mission_id),
        "status": "cancelled",
        "cascade_to_workers": body.cascade_to_workers,
    }
