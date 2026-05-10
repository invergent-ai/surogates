"""User-owned scheduled work API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel

from surogates.scheduled.models import ScheduledSession
from surogates.scheduled.store import ScheduledSessionStore
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

router = APIRouter()


class ScheduledWorkItem(BaseModel):
    id: UUID
    agent_id: str
    name: str | None = None
    prompt: str
    status: str
    kind: str
    source: str | None = None
    schedule_display: str
    timezone: str | None = None
    run_count: int
    repeat_limit: int | None = None
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_session_id: UUID | None = None
    last_error: str | None = None
    expires_at: datetime | None = None
    created_from_session_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class ScheduledWorkListResponse(BaseModel):
    items: list[ScheduledWorkItem]
    total: int


class RunScheduledWorkNowResponse(BaseModel):
    id: UUID
    queued: bool


def _scheduled_store(request: Request) -> ScheduledSessionStore:
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Scheduled work store is not available.",
        )
    return ScheduledSessionStore(session_factory)


def _require_user(tenant: TenantContext) -> UUID:
    if tenant.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Scheduled work is user-owned only.",
        )
    return tenant.user_id


def _agent_id(request: Request) -> str:
    return str(request.app.state.settings.agent_id)


def _kind(row: ScheduledSession) -> str:
    if row.schedule.get("kind") == "dynamic_loop":
        return "dynamic_loop"
    if row.repeat_limit == 1:
        return "one_shot"
    return "cron"


def _item(row: ScheduledSession) -> ScheduledWorkItem:
    return ScheduledWorkItem(
        id=row.id,
        agent_id=row.agent_id,
        name=row.name,
        prompt=row.prompt,
        status=row.status,
        kind=_kind(row),
        source=row.source,
        schedule_display=row.schedule_display,
        timezone=row.timezone,
        run_count=row.run_count,
        repeat_limit=row.repeat_limit,
        next_run_at=row.next_run_at,
        last_run_at=row.last_run_at,
        last_session_id=row.last_session_id,
        last_error=row.last_error,
        expires_at=row.expires_at,
        created_from_session_id=row.created_from_session_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/scheduled-work", response_model=ScheduledWorkListResponse)
async def list_scheduled_work(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    status_filter: Literal["active", "paused", "completed", "failed", "all"] = Query(
        "active",
        alias="status",
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> ScheduledWorkListResponse:
    user_id = _require_user(tenant)
    rows = await _scheduled_store(request).list_for_user(
        org_id=tenant.org_id,
        user_id=user_id,
        agent_id=_agent_id(request),
        status=status_filter,
        include_inactive=status_filter == "all",
        limit=limit,
        offset=offset,
    )
    return ScheduledWorkListResponse(
        items=[_item(row) for row in rows],
        total=len(rows),
    )


@router.post(
    "/scheduled-work/{schedule_id}/run-now",
    response_model=RunScheduledWorkNowResponse,
)
async def run_scheduled_work_now(
    schedule_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> RunScheduledWorkNowResponse:
    user_id = _require_user(tenant)
    queued = await _scheduled_store(request).run_now(
        org_id=tenant.org_id,
        user_id=user_id,
        agent_id=_agent_id(request),
        schedule_id=schedule_id,
    )
    if not queued:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Scheduled work {schedule_id} not found.",
        )
    return RunScheduledWorkNowResponse(id=schedule_id, queued=True)


@router.delete(
    "/scheduled-work/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def cancel_scheduled_work(
    schedule_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> Response:
    user_id = _require_user(tenant)
    deleted = await _scheduled_store(request).delete(
        org_id=tenant.org_id,
        user_id=user_id,
        agent_id=_agent_id(request),
        schedule_id=schedule_id,
    )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Scheduled work {schedule_id} not found.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
