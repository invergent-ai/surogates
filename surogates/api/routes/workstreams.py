"""Projects: the signed-in user's coordinator chats, kept as ``workstreams``.

Each project is a row and its master web session, the coordinator.  Every
route is scoped to the signed-in user and needs the agent's "multi session"
capability: with it off, every web session but the canonical one is hidden.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints

from surogates.runtime import AgentRuntimeContext, agent_runtime_context_dep
from surogates.session.provisioning import create_agent_session
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext
from surogates.workstreams import master_config
from surogates.workstreams.store import WorkstreamStore

router = APIRouter(prefix="/workstreams")

AgentRuntime = Annotated[AgentRuntimeContext, Depends(agent_runtime_context_dep)]
Tenant = Annotated[TenantContext, Depends(get_current_tenant)]


def _text(max_length: int, min_length: int = 1):
    def fits(value: str) -> str:
        # Counted in UTF-16 units, as the desktop shell counts them: an emoji is two.
        if len(value.encode("utf-16-le")) // 2 > max_length:
            raise ValueError(f"must be at most {max_length} characters")
        return value

    # Postgres text and jsonb refuse NUL, which would fail the write with a 500.
    return Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=min_length, pattern=r"^[^\x00]*$"),
        AfterValidator(fits),
    ]


def _blank_is_none(max_length: int):
    # A field the user emptied clears the value.
    return Annotated[_text(max_length, min_length=0), AfterValidator(lambda value: value or None)]


# The name is the master chat's title, whose cap is 256.
Name = _text(256)
Goal = _blank_is_none(2000)
Instructions = _text(16_000, min_length=0)


class ProjectCreate(BaseModel):
    name: Name
    goal: Goal | None = None
    instructions: Instructions = ""


class ProjectSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    icon: str | None
    created_at: datetime
    updated_at: datetime


class ProjectOut(ProjectSummaryOut):
    goal: str | None
    instructions: str
    master_session_id: UUID
    coordinator_tier: str | None
    thread_tier: str | None


def _owner(tenant: TenantContext, ctx: AgentRuntimeContext) -> dict:
    """The owner columns every project query is scoped to."""
    if tenant.user_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Projects belong to a user account.")
    if str(tenant.org_id) != ctx.org_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This agent belongs to another organization.")
    if not ctx.multi_session:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This agent keeps a single conversation, so it has no projects.",
        )
    return {"org_id": tenant.org_id, "agent_id": ctx.agent_id, "user_id": tenant.user_id}


def _store(request: Request) -> WorkstreamStore:
    return WorkstreamStore(request.app.state.session_factory)


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(body: ProjectCreate, request: Request, ctx: AgentRuntime, tenant: Tenant):
    owner = _owner(tenant, ctx)
    workstream_id = uuid4()
    sessions = request.app.state.session_store
    master = await create_agent_session(
        store=sessions,
        storage=request.app.state.storage,
        settings=request.app.state.settings,
        org_id=tenant.org_id,
        user_id=tenant.user_id,
        agent_id=ctx.agent_id,
        channel="web",
        config=master_config(workstream_id, name=body.name, goal=body.goal, instructions=body.instructions),
    )
    await sessions.update_session_title(master.id, body.name)
    try:
        return await _store(request).create(
            id=workstream_id, master_session_id=master.id,
            name=body.name, goal=body.goal, instructions=body.instructions, **owner,
        )
    except Exception:
        # A master with no project could be neither listed nor archived.
        await sessions.update_session_status(master.id, "archived")
        raise


@router.get("", response_model=list[ProjectSummaryOut])
async def list_projects(request: Request, ctx: AgentRuntime, tenant: Tenant):
    return await _store(request).list(**_owner(tenant, ctx))


@router.get("/{workstream_id}", response_model=ProjectOut)
async def get_project(workstream_id: UUID, request: Request, ctx: AgentRuntime, tenant: Tenant):
    project = await _store(request).get(workstream_id, **_owner(tenant, ctx))
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such project.")
    return project
