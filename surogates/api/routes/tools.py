"""Tool and MCP server listing endpoints."""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select

from surogates.db.models import McpServer, Skill
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class ToolInfo(BaseModel):
    id: UUID
    name: str
    description: str | None = None
    source: str  # "skill" or "mcp"
    enabled: bool
    created_at: datetime


class ToolListResponse(BaseModel):
    tools: list[ToolInfo]
    total: int


class SkillSummary(BaseModel):
    """Lightweight skill info for the frontend slash-command menu."""

    name: str
    description: str
    category: str | None = None
    trigger: str | None = None


class SkillListResponse(BaseModel):
    skills: list[SkillSummary]
    total: int


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/tools", response_model=ToolListResponse)
async def list_tools(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> ToolListResponse:
    """List available tools for the current tenant.

    This aggregates both tenant-scoped skills and MCP server definitions
    that are visible to the authenticated user.
    """
    session_factory = request.app.state.session_factory
    tools: list[ToolInfo] = []

    async with session_factory() as session:
        # Fetch org-level + user-level skills.
        skill_result = await session.execute(
            select(Skill).where(
                Skill.org_id == tenant.org_id,
                (Skill.user_id == tenant.user_id) | (Skill.user_id.is_(None)),
            )
        )
        skills = skill_result.scalars().all()

        for skill in skills:
            tools.append(
                ToolInfo(
                    id=skill.id,
                    name=skill.name,
                    description=skill.description,
                    source="skill",
                    enabled=skill.enabled,
                    created_at=skill.created_at,
                )
            )

        # Fetch org-level + user-level MCP servers.
        mcp_result = await session.execute(
            select(McpServer).where(
                McpServer.org_id == tenant.org_id,
                (McpServer.user_id == tenant.user_id)
                | (McpServer.user_id.is_(None)),
            )
        )
        mcp_servers = mcp_result.scalars().all()

        for server in mcp_servers:
            tools.append(
                ToolInfo(
                    id=server.id,
                    name=server.name,
                    description=None,
                    source="mcp",
                    enabled=server.enabled,
                    created_at=server.created_at,
                )
            )

    return ToolListResponse(tools=tools, total=len(tools))


@router.get("/skills", response_model=SkillListResponse)
async def list_skills(
    tenant: TenantContext = Depends(get_current_tenant),
) -> SkillListResponse:
    """List available skills from all layers (platform, org, user).

    Returns lightweight summaries suitable for the frontend
    slash-command menu.  Skills with a ``trigger`` field are
    intended to be invokable via ``/trigger-name``.
    """
    from surogates.tools.loader import ResourceLoader

    loader = ResourceLoader()
    skill_defs = loader.load_skills(tenant)

    summaries = [
        SkillSummary(
            name=s.name,
            description=s.description,
            category=s.category,
            trigger=s.trigger,
        )
        for s in skill_defs
    ]
    summaries.sort(key=lambda s: (s.category or "", s.name))

    return SkillListResponse(skills=summaries, total=len(summaries))
