"""User-owned scheduled work API."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from surogates.runtime import AgentRuntimeContext, agent_runtime_context_dep
from surogates.scheduled.models import ScheduledSession
from surogates.scheduled.prompt_guard import ScheduledPromptBlocked, validate_scheduled_prompt
from surogates.scheduled.schedule import schedule_from_fields, schedule_from_row
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
    cadence_kind: str | None = None
    cadence: dict | None = None
    paused_at: datetime | None = None
    ended_reason: str | None = None
    ended_by: str | None = None
    consecutive_failures: int = 0
    max_run_seconds: int | None = None


class ScheduledWorkListResponse(BaseModel):
    items: list[ScheduledWorkItem]
    total: int


class ScheduledWorkUpdate(BaseModel):
    """Fields to change; a new cadence is given as in ``cron_create``."""

    name: str | None = Field(None, max_length=200)
    prompt: str | None = Field(None, min_length=1)
    cron: str | None = None
    interval: str | None = None
    at: str | None = None
    days: list[str] | None = None
    times: list[str] | None = None
    timezone: str | None = None
    max_run_seconds: int | None = Field(None, ge=60, le=86400)


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


def _principal(tenant: TenantContext) -> dict[str, UUID | None]:
    """The caller as the store scopes schedules: a person sees their own, a
    service account the agent's."""
    if tenant.user_id is not None:
        return {"user_id": tenant.user_id, "service_account_id": None}
    service_account_id = getattr(tenant, "service_account_id", None)
    if service_account_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Scheduled work needs a signed-in user or a service account.",
        )
    return {"user_id": None, "service_account_id": service_account_id}


def _kind(row: ScheduledSession) -> str:
    if row.schedule.get("kind") == "dynamic_loop":
        return "dynamic_loop"
    if row.repeat_limit == 1 or row.kind == "once":
        return "one_shot"
    return "cron"


def _not_found(schedule_id: UUID) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Scheduled work {schedule_id} not found.",
    )


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
        cadence_kind=row.kind,
        cadence=row.cadence,
        paused_at=row.paused_at,
        ended_reason=row.ended_reason,
        ended_by=row.ended_by,
        consecutive_failures=row.consecutive_failures,
        max_run_seconds=row.max_run_seconds,
    )


@router.get("/scheduled-work", response_model=ScheduledWorkListResponse)
async def list_scheduled_work(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    agent_runtime: AgentRuntimeContext = Depends(agent_runtime_context_dep),
    status_filter: str = Query(
        "active",
        alias="status",
        pattern=r"^(all|(active|paused|completed|cancelled|failed)(,(active|paused|completed|cancelled|failed))*)$",
        description="One status, a comma list, or all.",
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    # A project's routines: the schedules its master session made.
    created_from_session_id: UUID | None = Query(None),
) -> ScheduledWorkListResponse:
    rows = await _scheduled_store(request).list_for_user(
        org_id=tenant.org_id,
        **_principal(tenant),
        agent_id=agent_runtime.agent_id,
        status=status_filter,
        include_inactive=status_filter == "all",
        limit=limit,
        offset=offset,
        created_from_session_id=created_from_session_id,
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
    agent_runtime: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> RunScheduledWorkNowResponse:
    queued = await _scheduled_store(request).run_now(
        org_id=tenant.org_id,
        **_principal(tenant),
        agent_id=agent_runtime.agent_id,
        schedule_id=schedule_id,
    )
    if not queued:
        raise _not_found(schedule_id)
    return RunScheduledWorkNowResponse(id=schedule_id, queued=True)


@router.get("/scheduled-work/{schedule_id}", response_model=ScheduledWorkItem)
async def get_scheduled_work(
    schedule_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    agent_runtime: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> ScheduledWorkItem:
    row = await _scheduled_store(request).get_for(
        schedule_id, org_id=tenant.org_id, agent_id=agent_runtime.agent_id, **_principal(tenant),
    )
    if row is None:
        raise _not_found(schedule_id)
    return _item(row)


@router.patch("/scheduled-work/{schedule_id}", response_model=ScheduledWorkItem)
async def update_scheduled_work(
    schedule_id: UUID,
    body: ScheduledWorkUpdate,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    agent_runtime: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> ScheduledWorkItem:
    store = _scheduled_store(request)
    principal = _principal(tenant)
    current = await store.get_for(
        schedule_id, org_id=tenant.org_id, agent_id=agent_runtime.agent_id, **principal,
    )
    if current is None or current.status not in ("active", "paused"):
        raise _not_found(schedule_id)
    try:
        if body.prompt is not None:
            validate_scheduled_prompt(body.prompt, source="scheduled_work")
        cadence = None
        if any(v for v in (body.cron, body.interval, body.at, body.days, body.times)):
            cadence = schedule_from_fields(
                cron=body.cron, interval=body.interval, at=body.at, days=body.days, times=body.times,
                timezone_name=body.timezone or current.timezone or "UTC",
            )
        elif body.timezone is not None:
            cadence = schedule_from_row(current.model_copy(update={
                "timezone": body.timezone,
                "cadence": {**(current.cadence or {}), "tz": body.timezone},
                "schedule": {**current.schedule, "timezone_name": body.timezone},
            }))
        updated = await store.update(
            org_id=tenant.org_id, agent_id=agent_runtime.agent_id, schedule_id=schedule_id,
            name=body.name, prompt=body.prompt, schedule=cadence,
            max_run_seconds=body.max_run_seconds, **principal,
        )
    except (ValueError, ScheduledPromptBlocked) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    if updated is None:
        raise _not_found(schedule_id)
    return _item(updated)


@router.post("/scheduled-work/{schedule_id}/pause", response_model=ScheduledWorkItem)
async def pause_scheduled_work(
    schedule_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    agent_runtime: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> ScheduledWorkItem:
    return await _transition(request, tenant, agent_runtime, schedule_id, "pause")


@router.post("/scheduled-work/{schedule_id}/resume", response_model=ScheduledWorkItem)
async def resume_scheduled_work(
    schedule_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    agent_runtime: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> ScheduledWorkItem:
    return await _transition(request, tenant, agent_runtime, schedule_id, "resume")


async def _transition(
    request: Request, tenant: TenantContext, agent_runtime: AgentRuntimeContext,
    schedule_id: UUID, action: str,
) -> ScheduledWorkItem:
    store = _scheduled_store(request)
    principal = _principal(tenant)
    changed = await getattr(store, action)(
        org_id=tenant.org_id, agent_id=agent_runtime.agent_id, schedule_id=schedule_id, **principal,
    )
    row = await store.get_for(
        schedule_id, org_id=tenant.org_id, agent_id=agent_runtime.agent_id, **principal,
    )
    if row is None:
        raise _not_found(schedule_id)
    if not changed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Scheduled work {schedule_id} is {row.status}; it cannot {action}.",
        )
    return _item(row)


@router.delete(
    "/scheduled-work/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def cancel_scheduled_work(
    schedule_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    agent_runtime: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> Response:
    """Cancel: the row is kept, with who cancelled it; its run still going is stopped."""
    store = _scheduled_store(request)
    principal = _principal(tenant)
    row = await store.get_for(
        schedule_id, org_id=tenant.org_id, agent_id=agent_runtime.agent_id, **principal,
    )
    if row is None:
        raise _not_found(schedule_id)
    await store.cancel(
        org_id=tenant.org_id, agent_id=agent_runtime.agent_id, schedule_id=schedule_id,
        ended_by=_who(tenant), **principal,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _who(tenant: TenantContext) -> str:
    if tenant.user_id is not None:
        return f"user:{tenant.user_id}"
    return f"service_account:{getattr(tenant, 'service_account_id', None)}"
