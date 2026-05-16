"""FastAPI routes for the missions REST surface.

Read-only GET endpoints in this module; POST endpoints (pause, resume,
cancel) are added in Task 12 of the implementation plan.

Auth: every route depends on :func:`get_current_tenant` to extract
``org_id`` and ``user_id`` from the bearer token. Mission rows are
filtered to ``(tenant.org_id, tenant.user_id)`` so cross-tenant access
returns 404 (not 403) — the same shape as ``sessions.py``.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from surogates.db.models import (
    Event,
    Mission as MissionRow,
    Session as ORMSession,
    Task as TaskRow,
    TaskLink,
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


async def _load_mission_authorized(
    mission_id: UUID,
    *,
    session_factory: async_sessionmaker,
    tenant: TenantContext,
) -> MissionRow:
    """Fetch a mission row and authorize against the request's tenant."""
    async with session_factory() as db:
        row = await db.get(MissionRow, mission_id)
        if row is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"mission {mission_id} not found",
            )
        if row.org_id != tenant.org_id or row.user_id != tenant.user_id:
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
    statuses = [s.strip() for s in status_filter.split(",") if s.strip()]
    async with session_factory() as db:
        stmt = (
            select(MissionRow)
            .where(
                MissionRow.org_id == tenant.org_id,
                MissionRow.user_id == tenant.user_id,
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


@router.get("/{mission_id}/tasks")
async def get_mission_tasks(
    mission_id: UUID,
    session_factory: async_sessionmaker = Depends(_session_factory_dep),
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict[str, Any]:
    await _load_mission_authorized(
        mission_id, session_factory=session_factory, tenant=tenant,
    )
    async with session_factory() as db:
        tasks = (await db.execute(
            select(TaskRow).where(TaskRow.mission_id == mission_id)
            .order_by(TaskRow.created_at.asc())
        )).scalars().all()
        # Resolve parent edges for the task DAG view. Querying with an
        # empty IN-list would generate a no-op SELECT, so short-circuit.
        links: list[TaskLink] = []
        if tasks:
            links = (await db.execute(
                select(TaskLink).where(
                    TaskLink.child_id.in_([t.id for t in tasks]),
                )
            )).scalars().all()
    parent_ids_by_child: dict[str, list[str]] = {}
    for link in links:
        parent_ids_by_child.setdefault(str(link.child_id), []).append(
            str(link.parent_id),
        )

    payload = []
    for t in tasks:
        payload.append({
            "id": str(t.id),
            "goal": t.goal,
            "status": t.status,
            "attempt_count": t.attempt_count,
            "max_attempts": t.max_attempts,
            "agent_def_name": t.agent_def_name,
            "result": t.result,
            "result_metadata": t.result_metadata,
            "parent_ids": parent_ids_by_child.get(str(t.id), []),
            "current_session_id": (
                str(t.current_session_id) if t.current_session_id else None
            ),
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "completed_at": (
                t.completed_at.isoformat() if t.completed_at else None
            ),
        })
    return {"tasks": payload}


@router.get("/{mission_id}/workers")
async def get_mission_workers(
    mission_id: UUID,
    session_factory: async_sessionmaker = Depends(_session_factory_dep),
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict[str, Any]:
    """Return live/recent worker activity rows for the mission.

    The client derives a human-friendly activity label from the
    ``latest_event_*`` fields; the server's job is just to expose them.
    """
    await _load_mission_authorized(
        mission_id, session_factory=session_factory, tenant=tenant,
    )
    async with session_factory() as db:
        tasks = (await db.execute(
            select(TaskRow).where(
                TaskRow.mission_id == mission_id,
                TaskRow.current_session_id.isnot(None),
            )
        )).scalars().all()

        workers: list[dict[str, Any]] = []
        for t in tasks:
            sess = await db.get(ORMSession, t.current_session_id)
            if sess is None:
                continue
            latest = (await db.execute(
                select(Event)
                .where(Event.session_id == sess.id)
                .order_by(Event.id.desc())
                .limit(1)
            )).scalar_one_or_none()
            workers.append({
                "task_id": str(t.id),
                "worker_session_id": str(sess.id),
                "agent_def_name": t.agent_def_name,
                "task_status": t.status,
                "session_status": sess.status,
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
                "transcript_url": f"/chat/{sess.id}",
            })
    return {"workers": workers}


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
        session_id=row.session_id, agent_id=row.agent_id,
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
