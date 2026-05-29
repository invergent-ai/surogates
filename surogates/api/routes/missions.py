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
    """Return worker activity rows for the mission.

    Returns three categories of children, distinguished by ``kind``:

    * ``"task"`` — task-backed workers (the coordinator called ``spawn_task``).
      Carry a ``task_id`` and a ``task_status``; mature, durable, retried by
      the dispatcher.
    * ``"worker"`` — async one-shot children spawned via ``spawn_worker``.
      No Task row, but the session is durable. ``task_id``/``task_status``
      are ``None``.
    * ``"delegation"`` — sync fork-join children spawned via
      ``delegate_task``. The coordinator's wake blocks until they finish;
      sessions are usually short-lived. Same null-task shape as workers.

    Direct ``spawn_worker``/``delegate_task`` children were previously
    invisible — they have no Task row, so the old task-driven query
    returned an empty list even though the coordinator was actively
    delegating. Merging them in surfaces real activity without changing
    agent behaviour.

    The client derives a human-friendly activity label from the
    ``latest_event_*`` fields; the server's job is just to expose them.
    """
    mission_row = await _load_mission_authorized(
        mission_id, session_factory=session_factory, tenant=tenant,
    )
    workers: list[dict[str, Any]] = []
    async with session_factory() as db:
        # 1. Task-backed workers (the spawn_task path).
        tasks = (await db.execute(
            select(TaskRow).where(
                TaskRow.mission_id == mission_id,
                TaskRow.current_session_id.isnot(None),
            )
        )).scalars().all()
        for t in tasks:
            sess = await db.get(ORMSession, t.current_session_id)
            if sess is None:
                continue
            workers.append(await _worker_row(
                db, kind="task", task=t, session=sess,
            ))

        # 2. Direct children of the coordinator's session (spawn_worker +
        # delegate_task). These don't have Task rows.  Channel discriminates:
        #   * ``worker`` — spawn_worker
        #   * ``delegation`` — delegate_task
        # Filtering to those two channels keeps unrelated child shapes
        # (``scheduled``, ``api``, etc.) out — only the coordinator-driven
        # primitives surface here.
        direct_children = (await db.execute(
            select(ORMSession).where(
                ORMSession.parent_id == mission_row.session_id,
                ORMSession.channel.in_(("worker", "delegation")),
            ).order_by(ORMSession.created_at.asc())
        )).scalars().all()
        for sess in direct_children:
            kind = "worker" if sess.channel == "worker" else "delegation"
            workers.append(await _worker_row(
                db, kind=kind, task=None, session=sess,
            ))
    return {"workers": workers}


async def _worker_row(
    db,
    *,
    kind: str,
    task: TaskRow | None,
    session: ORMSession,
) -> dict[str, Any]:
    """Build one worker entry, joining the session's latest event."""
    latest = (await db.execute(
        select(Event)
        .where(Event.session_id == session.id)
        .order_by(Event.id.desc())
        .limit(1)
    )).scalar_one_or_none()
    return {
        "kind": kind,
        "task_id": str(task.id) if task is not None else None,
        "worker_session_id": str(session.id),
        "agent_def_name": (
            task.agent_def_name if task is not None
            else (session.config or {}).get("agent_def_name")
        ),
        "task_status": task.status if task is not None else None,
        "session_status": session.status,
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
        "transcript_url": f"/chat/{session.id}",
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
