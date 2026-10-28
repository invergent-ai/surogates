"""Scheduled work API: what a person, the agent's operators (through the
control plane) and integrations read and change about an agent's schedules."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from surogates.runtime import AgentRuntimeContext, agent_runtime_context_dep
from surogates.runtime.agent_principal import find_agent_principal
from surogates.scheduled.materialize import stop_runs_of_cancelled
from surogates.scheduled.models import ScheduledSession
from surogates.scheduled.prompt_guard import ScheduledPromptBlocked, validate_scheduled_prompt
from surogates.scheduled.schedule import (
    ParsedSchedule,
    schedule_from_cadence,
    schedule_from_fields,
    schedule_from_row,
    upcoming_runs,
)
from surogates.scheduled.store import ScheduledSessionStore
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext
from surogates.tools.owner_scope import is_ops_chat_service_account

router = APIRouter()

MIN_RUN_SECONDS = 60
MAX_RUN_SECONDS = 86400
_STATUSES = "active|paused|completed|cancelled|failed"
#: The context keys only an operator's ops-chat account may set on create.
_OPERATOR_CONTEXT_KEYS = ("ops", "operator")


class Cadence(BaseModel):
    """When a schedule runs, as the Studio form gives it."""

    kind: Literal["calendar", "interval", "once", "cron"]
    days: list[int | str] | None = None
    times: list[str] | None = None
    tz: str | None = None
    seconds: int | None = None
    at: datetime | None = None
    cron: str | None = None

    def parsed(self) -> ParsedSchedule:
        return schedule_from_cadence(
            self.kind, days=self.days, times=self.times, tz=self.tz,
            seconds=self.seconds, at=self.at, cron=self.cron,
        )


class RunsLoggedInAs(BaseModel):
    profile_id: UUID
    name: str | None = None
    owner: Literal["me", "agent"]
    missing: bool = False


class ScheduledWorkItem(BaseModel):
    id: UUID
    agent_id: str
    name: str | None = None
    prompt: str
    status: str
    kind: str
    source: str | None = None
    display: str
    schedule_display: str
    timezone: str | None = None
    run_count: int
    repeat_limit: int | None = None
    next_run_at: datetime | None = None
    next_fires: list[datetime] = []
    last_run_at: datetime | None = None
    last_session_id: UUID | None = None
    last_error: str | None = None
    expires_at: datetime | None = None
    created_from_session_id: UUID | None = None
    created_at: datetime
    updated_at: datetime
    cadence: dict | None = None
    paused_at: datetime | None = None
    ended_reason: str | None = None
    ended_by: str | None = None
    consecutive_failures: int = 0
    max_run_seconds: int | None = None
    owner_user_id: UUID | None = None
    owner_service_account_id: UUID | None = None
    owner_user_name: str | None = None
    owner_service_account_name: str | None = None
    browser_profile_id: UUID | None = None
    runs_logged_in_as: RunsLoggedInAs | None = None


class ScheduledWorkListResponse(BaseModel):
    items: list[ScheduledWorkItem]
    total: int


class ScheduledWorkCreate(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)
    name: str = Field("", max_length=200)
    cadence: Cadence
    browser_profile_id: UUID | None = None
    max_run_seconds: int | None = Field(None, ge=MIN_RUN_SECONDS, le=MAX_RUN_SECONDS)
    context: dict[str, Any] | None = None


class ScheduledWorkUpdate(BaseModel):
    """Fields left out stay as they are.  ``browser_profile_id: null`` runs
    logged out; ``max_run_seconds: 0`` removes the limit.  The flat cadence
    fields are read as in ``cron_create``."""

    name: str | None = Field(None, max_length=200)
    prompt: str | None = Field(None, min_length=1, max_length=20_000)
    cadence: Cadence | None = None
    browser_profile_id: UUID | None = None
    max_run_seconds: int | None = None
    cron: str | None = None
    interval: str | None = None
    at: str | None = None
    days: list[str] | None = None
    times: list[str] | None = None
    timezone: str | None = None


class CadencePreviewRequest(BaseModel):
    cadence: Cadence


class CadencePreview(BaseModel):
    display: str
    kind: str
    timezone: str
    cadence: dict
    next_fires: list[datetime]


class ScheduledRun(BaseModel):
    id: UUID
    status: str
    title: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class ScheduledRunsResponse(BaseModel):
    items: list[ScheduledRun]


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


def _visible_to(tenant: TenantContext) -> dict[str, Any]:
    """The caller, for a read: its principal, and a Keel case's schedules
    hidden from all but that case session's own token."""
    return {**_principal(tenant), "hide_cases": True,
            "case_session_id": getattr(tenant, "session_scope_id", None)}


async def _visible(
    request: Request, tenant: TenantContext, agent_id: str, schedule_id: UUID,
) -> ScheduledSession:
    row = await _scheduled_store(request).get_for(
        schedule_id, org_id=tenant.org_id, agent_id=agent_id, **_visible_to(tenant),
    )
    if row is None:
        raise _not_found(schedule_id)
    return row


def _principal(tenant: TenantContext) -> dict[str, UUID | None]:
    """The caller as the store scopes schedules (``user_id`` /
    ``service_account_id``)."""
    if tenant.user_id is not None:
        return {"user_id": tenant.user_id, "service_account_id": None}
    service_account_id = getattr(tenant, "service_account_id", None)
    if service_account_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Scheduled work needs a signed-in user or a service account.",
        )
    return {"user_id": None, "service_account_id": service_account_id}


async def _is_operator(request: Request, tenant: TenantContext) -> bool:
    """Whether the caller is an operator's ops-chat account (Studio)."""
    if tenant.user_id is not None:
        return False
    return await is_ops_chat_service_account(
        request.app.state.session_store, getattr(tenant, "service_account_id", None),
    )


def _unprocessable(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


def _not_found(schedule_id: UUID) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Scheduled work {schedule_id} not found.",
    )


def _profile_store(request: Request) -> Any:
    return getattr(request.app.state, "browser_profile_store", None)


async def _agent_principal_id(request: Request, org_id: UUID, agent_id: str) -> UUID | None:
    principal = await find_agent_principal(request.app.state.session_factory, org_id, agent_id)
    return principal.id if principal is not None else None


async def _items(
    request: Request, rows: list[ScheduledSession], org_id: UUID, agent_id: str,
) -> list[ScheduledWorkItem]:
    """Items with their owners' names and whose login their runs use,
    looked up once for all rows."""
    user_names, account_names = await _scheduled_store(request).owner_names(rows)
    bound = {
        row.id: _bound_profile(row) for row in rows if _bound_profile(row) is not None
    }
    profiles = _profile_store(request)
    owners = await profiles.owners(org_id, list(set(bound.values()))) if profiles and bound else {}
    agent_sa = await _agent_principal_id(request, org_id, agent_id) if profiles else None
    agent_login = None
    if agent_sa is not None and any(row.id not in bound for row in rows):
        found = await profiles.used_in_channel(org_id, service_account_id=agent_sa, channel="scheduled")
        if found is not None:
            agent_login = (await profiles.owners(org_id, [found])).get(found)
    items = []
    for row in rows:
        login: RunsLoggedInAs | None = None
        pid = bound.get(row.id)
        if pid is not None:
            owner = owners.get(pid)
            login = RunsLoggedInAs(
                profile_id=pid,
                name=owner[0].name if owner else None,
                owner="agent" if owner and agent_sa is not None and owner[2] == agent_sa else "me",
                missing=owner is None or not owner[0].has_state,
            )
        elif agent_login is not None:
            login = RunsLoggedInAs(profile_id=agent_login[0].id, name=agent_login[0].name, owner="agent")
        items.append(_item(row, login).model_copy(update={
            "owner_user_name": user_names.get(row.user_id),
            "owner_service_account_name": account_names.get(row.service_account_id),
        }))
    return items


def _bound_profile(row: ScheduledSession) -> UUID | None:
    raw = ((row.context or {}).get("browser") or {}).get("profile_id")
    try:
        return UUID(str(raw)) if raw else None
    except ValueError:
        return None


def _item(row: ScheduledSession, login: RunsLoggedInAs | None) -> ScheduledWorkItem:
    try:
        fires = upcoming_runs(row)
    except ValueError:
        fires = []
    return ScheduledWorkItem(
        **row.model_dump(include=set(ScheduledWorkItem.model_fields) - {"kind"}),
        kind=row.kind or schedule_from_row(row).kind,
        display=row.schedule_display,
        next_fires=fires,
        owner_user_id=row.user_id,
        owner_service_account_id=row.service_account_id,
        browser_profile_id=_bound_profile(row),
        runs_logged_in_as=login,
    )


async def _one(request: Request, row: ScheduledSession) -> ScheduledWorkItem:
    [item] = await _items(request, [row], row.org_id, row.agent_id)
    return item


async def _check_profile(
    request: Request, tenant: TenantContext, agent_id: str, profile_id: UUID,
    *, owner: dict[str, UUID | None], operator: bool,
) -> None:
    """A schedule may run as its owner's own profile or, when an operator
    sets it, the agent's.  Anyone else's login is never borrowed."""
    profiles = _profile_store(request)
    if profiles is None:
        raise HTTPException(status_code=503, detail="Browser profiles unavailable.")
    found = (await profiles.owners(tenant.org_id, [profile_id])).get(profile_id)
    if found is not None:
        _row, user_id, sa_id = found
        if (user_id, sa_id) == (owner["user_id"], owner["service_account_id"]):
            return
        if operator and sa_id is not None and sa_id == await _agent_principal_id(
            request, tenant.org_id, agent_id,
        ):
            return
    raise _unprocessable(ValueError("That browser profile is not one this schedule may use."))


def _with_profile(context: dict[str, Any], profile_id: UUID | None) -> dict[str, Any]:
    browser = {k: v for k, v in (context.get("browser") or {}).items() if k != "profile_id"}
    if profile_id is not None:
        browser["profile_id"] = str(profile_id)
    context = {k: v for k, v in context.items() if k != "browser"}
    return {**context, "browser": browser} if browser else context


def _who(tenant: TenantContext) -> str:
    if tenant.user_id is not None:
        return f"user:{tenant.user_id}"
    return f"service_account:{getattr(tenant, 'service_account_id', None)}"


@router.get("/scheduled-work", response_model=ScheduledWorkListResponse)
async def list_scheduled_work(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    agent_runtime: AgentRuntimeContext = Depends(agent_runtime_context_dep),
    status_filter: str = Query(
        "active",
        alias="status",
        pattern=rf"^(all|live|({_STATUSES})(,({_STATUSES}))*)$",
        description="One status, a comma list, live (active and paused), or all.",
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    # A project's routines: the schedules its master session made.
    created_from_session_id: UUID | None = Query(None),
) -> ScheduledWorkListResponse:
    rows = await _scheduled_store(request).list_for_user(
        org_id=tenant.org_id,
        **_visible_to(tenant),
        agent_id=agent_runtime.agent_id,
        status="active,paused" if status_filter == "live" else status_filter,
        limit=limit,
        offset=offset,
        created_from_session_id=created_from_session_id,
    )
    items = await _items(request, rows, tenant.org_id, agent_runtime.agent_id)
    return ScheduledWorkListResponse(items=items, total=len(items))


@router.post("/scheduled-work/preview", response_model=CadencePreview)
async def preview_scheduled_work(
    body: CadencePreviewRequest,
    tenant: TenantContext = Depends(get_current_tenant),
) -> CadencePreview:
    """How a cadence reads and when it would run, before anything is saved."""
    _principal(tenant)
    try:
        cadence = body.cadence.parsed()
        now = datetime.now().astimezone()
        cadence = cadence.anchored(now)
        first = cadence.next_after(now)
        if first is None:
            raise ValueError("That time has already passed.")
    except ValueError as exc:
        raise _unprocessable(exc) from exc
    fires = [first]
    while len(fires) < 3 and (following := cadence.next_after(fires[-1])) is not None:
        fires.append(following)
    return CadencePreview(
        display=cadence.display, kind=cadence.kind, timezone=cadence.timezone_name,
        cadence=cadence.cadence(), next_fires=fires,
    )


@router.post(
    "/scheduled-work", response_model=ScheduledWorkItem, status_code=status.HTTP_201_CREATED,
)
async def create_scheduled_work(
    body: ScheduledWorkCreate,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    agent_runtime: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> ScheduledWorkItem:
    """A schedule owned by the caller.  Only an operator's ops-chat account
    may say which operator and project it acts for, and bind the agent's login."""
    principal = _principal(tenant)
    operator = await _is_operator(request, tenant)
    context: dict[str, Any] = {}
    if operator and body.context:
        context = {k: body.context[k] for k in _OPERATOR_CONTEXT_KEYS if k in body.context}
    if body.browser_profile_id is not None:
        await _check_profile(
            request, tenant, agent_runtime.agent_id, body.browser_profile_id,
            owner=principal, operator=operator,
        )
        context = _with_profile(context, body.browser_profile_id)
    try:
        validate_scheduled_prompt(body.prompt, source="scheduled_work")
        cadence = body.cadence.parsed()
        row = await _scheduled_store(request).create(
            org_id=tenant.org_id, agent_id=agent_runtime.agent_id, **principal,
            name=body.name.strip() or body.prompt.strip()[:80], prompt=body.prompt,
            schedule=cadence, source="studio" if operator else "api",
            created_from_session_id=None,
            repeat_limit=1 if cadence.kind == "once" else None,
            context=context, max_run_seconds=body.max_run_seconds,
        )
    except (ValueError, ScheduledPromptBlocked) as exc:
        raise _unprocessable(exc) from exc
    return await _one(request, row)


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
    await _visible(request, tenant, agent_runtime.agent_id, schedule_id)
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
    return await _one(request, await _visible(request, tenant, agent_runtime.agent_id, schedule_id))


@router.get("/scheduled-work/{schedule_id}/runs", response_model=ScheduledRunsResponse)
async def list_scheduled_work_runs(
    schedule_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    agent_runtime: AgentRuntimeContext = Depends(agent_runtime_context_dep),
    limit: int = Query(20, ge=1, le=100),
) -> ScheduledRunsResponse:
    await _visible(request, tenant, agent_runtime.agent_id, schedule_id)
    runs = await _scheduled_store(request).list_runs(
        schedule_id, org_id=tenant.org_id, agent_id=agent_runtime.agent_id, limit=limit,
    )
    return ScheduledRunsResponse(items=[ScheduledRun(**run) for run in runs])


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
    current = await _visible(request, tenant, agent_runtime.agent_id, schedule_id)
    if current.status not in ("active", "paused"):
        raise _not_found(schedule_id)
    if body.max_run_seconds not in (None, 0) and not (
        MIN_RUN_SECONDS <= body.max_run_seconds <= MAX_RUN_SECONDS
    ):
        raise _unprocessable(ValueError(
            f"A run time limit is {MIN_RUN_SECONDS} to {MAX_RUN_SECONDS} seconds, or 0 for none.",
        ))
    context = None
    if "browser_profile_id" in body.model_fields_set:
        if body.browser_profile_id is not None:
            await _check_profile(
                request, tenant, agent_runtime.agent_id, body.browser_profile_id,
                owner={"user_id": current.user_id, "service_account_id": current.service_account_id},
                operator=await _is_operator(request, tenant),
            )
        context = _with_profile(dict(current.context or {}), body.browser_profile_id)
    try:
        if body.prompt is not None:
            validate_scheduled_prompt(body.prompt, source="scheduled_work")
        cadence = _new_cadence(body, current)
        updated = await store.update(
            org_id=tenant.org_id, agent_id=agent_runtime.agent_id, schedule_id=schedule_id,
            name=body.name, prompt=body.prompt, schedule=cadence, context=context,
            max_run_seconds=body.max_run_seconds, **principal,
        )
    except (ValueError, ScheduledPromptBlocked) as exc:
        raise _unprocessable(exc) from exc
    if updated is None:
        raise _not_found(schedule_id)
    return await _one(request, updated)


def _new_cadence(body: ScheduledWorkUpdate, current: ScheduledSession) -> ParsedSchedule | None:
    if body.cadence is not None:
        return body.cadence.parsed()
    if any(v for v in (body.cron, body.interval, body.at, body.days, body.times)):
        return schedule_from_fields(
            cron=body.cron, interval=body.interval, at=body.at, days=body.days, times=body.times,
            timezone_name=body.timezone or current.timezone or "UTC",
        )
    if body.timezone is not None:
        return schedule_from_row(current.model_copy(update={
            "timezone": body.timezone,
            "cadence": {**(current.cadence or {}), "tz": body.timezone},
        }))
    return None


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
    await _visible(request, tenant, agent_runtime.agent_id, schedule_id)
    changed, row = await _scheduled_store(request).apply(
        action, org_id=tenant.org_id, agent_id=agent_runtime.agent_id, schedule_id=schedule_id,
        **_principal(tenant),
    )
    if row is None:
        raise _not_found(schedule_id)
    if not changed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Scheduled work {schedule_id} is {row.status}; it cannot {action}.",
        )
    return await _one(request, row)


@router.delete(
    "/scheduled-work/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def cancel_scheduled_work(
    schedule_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    agent_runtime: AgentRuntimeContext = Depends(agent_runtime_context_dep),
    ended_by: str | None = Query(None, max_length=200, pattern=r"^ops_user:\S+$"),
) -> Response:
    """Cancel: the row is kept, with who cancelled it, and its runs still
    going are stopped now.  Only an operator's account names the Studio user."""
    await _visible(request, tenant, agent_runtime.agent_id, schedule_id)
    store = _scheduled_store(request)
    principal = _principal(tenant)
    if ended_by is not None and not await _is_operator(request, tenant):
        ended_by = None
    if await store.cancel(
        org_id=tenant.org_id, agent_id=agent_runtime.agent_id, schedule_id=schedule_id,
        ended_by=ended_by or _who(tenant), **principal,
    ):
        await stop_runs_of_cancelled(
            store, request.app.state.session_store, getattr(request.app.state, "redis", None),
            schedule_id=schedule_id,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
