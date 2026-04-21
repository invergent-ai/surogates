"""Sub-agent REST API -- CRUD operations on declarative sub-agent types.

A sub-agent type is a preset bundle of (system prompt, tool filter,
model, iteration cap, governance policy profile) referenced by name
when a coordinator spawns a worker with ``agent_type=<name>``.  Types
are merged from four layers by
:class:`~surogates.tools.loader.ResourceLoader`:

1. Platform filesystem (``/etc/surogates/agents/``)
2. User bucket files (``tenant-{org}/users/{user}/agents/``)
3. Org-wide DB rows (``agents`` table, ``user_id IS NULL``)
4. User-specific DB rows (``agents`` table)

This route handles the bucket-backed layers (2) by writing AGENT.md
files into the tenant bucket via :class:`TenantStorage`.  Platform
agents are read-only and surface in ``GET /agents`` without a
corresponding POST/PUT/DELETE path.  DB-overlay management is handled
by admin tooling, not this public API.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from surogates.api.routes._shared import normalize_source, raise_validation
from surogates.storage.tenant import TenantStorage
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext
from surogates.tools.loader import (
    AGENT_SOURCE_ORG_DB,
    AGENT_SOURCE_PLATFORM,
    AGENT_SOURCE_USER_DB,
    ResourceLoader,
    _build_agent_def,
    _parse_agent_frontmatter,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Response / request schemas
# ---------------------------------------------------------------------------


class AgentSummary(BaseModel):
    """Lightweight sub-agent info for listing in UIs."""

    name: str
    description: str
    source: str = "platform"  # "platform", "org", or "user"
    category: str | None = None
    model: str | None = None
    max_iterations: int | None = None
    policy_profile: str | None = None
    enabled: bool = True


class AgentListResponse(BaseModel):
    agents: list[AgentSummary]
    total: int


class AgentDetail(BaseModel):
    """Full sub-agent definition."""

    name: str
    description: str
    source: str
    system_prompt: str
    tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    model: str | None = None
    max_iterations: int | None = None
    policy_profile: str | None = None
    category: str | None = None
    tags: list[str] | None = None
    enabled: bool = True


class CreateAgentRequest(BaseModel):
    name: str
    content: str  # full AGENT.md body including YAML frontmatter
    category: str | None = None


class EditAgentRequest(BaseModel):
    content: str


class AgentActionResponse(BaseModel):
    success: bool
    message: str
    category: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_tenant_storage(request: Request, tenant: TenantContext) -> TenantStorage:
    """Create a TenantStorage for the current request."""
    return TenantStorage(
        backend=request.app.state.storage,
        org_id=tenant.org_id,
        user_id=tenant.user_id,
    )


def _validate_agent_name(name: str) -> str | None:
    """Basic name validation: non-empty, no path separators."""
    if not name or not name.strip():
        return "Agent name must not be empty."
    if "/" in name or "\\" in name or ".." in name:
        return "Agent name must not contain path separators."
    if len(name) > 128:
        return "Agent name must be 128 characters or less."
    return None


def _validate_content(content: str) -> str | None:
    """AGENT.md content validation: must include frontmatter."""
    if not content or not content.strip():
        return "AGENT.md content must not be empty."
    if len(content.encode("utf-8")) > 256_000:
        return "AGENT.md content exceeds 256KB limit."
    if not content.lstrip().startswith("---"):
        return "AGENT.md must begin with a YAML frontmatter block (---)."
    return None


def _validate_name_matches_frontmatter(
    request_name: str, content: str,
) -> str | None:
    """Ensure the request name matches the AGENT.md frontmatter name.

    Writing under ``request_name`` while the frontmatter says something
    else would produce a ghost agent whose storage path disagrees with
    its catalog listing -- ``DELETE /agents/{name}`` would fail to
    locate it.  Parsing the frontmatter here is cheap and gives a
    targeted 422 instead of a silent split-brain later.
    """
    parsed = _parse_agent_frontmatter(content, request_name)
    fm_name = parsed.get("name")
    if fm_name and fm_name != request_name:
        return (
            f"Request name {request_name!r} does not match AGENT.md "
            f"frontmatter name {fm_name!r}."
        )
    return None


def _validate_category(category: str | None) -> str | None:
    """Category validation: optional; no path traversal."""
    if category is None:
        return None
    if not category.strip():
        return "Category must not be blank."
    if "/" in category or "\\" in category or ".." in category:
        return "Category must not contain path separators."
    if len(category) > 64:
        return "Category must be 64 characters or less."
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def _load_bucket_agents(ts: TenantStorage) -> list[Any]:
    """Load sub-agents from the tenant bucket as :class:`AgentDef` objects.

    The :class:`ResourceLoader` filesystem layer follows a path
    convention (``{asset_root}/{org_id}/users/...``) that does not
    align with :class:`TenantStorage`'s bucket layout
    (``tenant-{org_id}/users/...``).  To make POST/GET symmetrical for
    bucket-stored agents we list via :class:`TenantStorage` and parse
    AGENT.md frontmatter directly, yielding fully-populated
    :class:`AgentDef` instances with ``source="user"`` or ``"org"``.
    """
    entries = await ts.list_all_agents()
    # Fetch AGENT.md contents in parallel.  For a tenant with N agents
    # this collapses N sequential S3 GETs into a single round-trip
    # window, which is the dominant cost on object-storage backends.
    contents = await asyncio.gather(
        *(ts.read_agent(e["key_prefix"]) for e in entries),
        return_exceptions=True,
    )
    defs: list[Any] = []
    for entry, content in zip(entries, contents):
        if isinstance(content, BaseException):
            if not isinstance(content, KeyError):
                logger.warning(
                    "Failed to read AGENT.md at %s", entry["key_prefix"],
                    exc_info=content,
                )
            continue
        try:
            parsed = _parse_agent_frontmatter(content, entry["name"])
            defs.append(_build_agent_def(parsed, entry["layer"]))
        except Exception:
            logger.warning(
                "Failed to parse AGENT.md at %s", entry["key_prefix"],
                exc_info=True,
            )
    return defs


async def _merged_agent_catalog(
    request: Request, tenant: TenantContext,
) -> list[Any]:
    """Merge platform-filesystem + DB + tenant-bucket sub-agent layers.

    Precedence (lowest → highest): platform < bucket (user/org) < org_db
    < user_db.  The loader handles platform + DB layers; bucket layers
    are discovered via :class:`TenantStorage` to match the write path
    used by POST/PUT/DELETE on this route.
    """
    loader = ResourceLoader()
    session_factory = request.app.state.session_factory
    async with session_factory() as db_session:
        # Platform + DB layers via the loader.  Pass db_session so the
        # DB overlay layers are included.
        loader_defs = await loader.load_agents(
            tenant, db_session=db_session,
        )

    ts = _get_tenant_storage(request, tenant)
    try:
        bucket_defs = await _load_bucket_agents(ts)
    except Exception:
        logger.debug(
            "No bucket-stored agents found for tenant %s",
            tenant.org_id, exc_info=True,
        )
        bucket_defs = []

    # Partition the loader defs by source.  Platform goes beneath the
    # bucket layer; DB layers stay on top.
    platform_defs = [d for d in loader_defs if d.source == AGENT_SOURCE_PLATFORM]
    db_defs = [
        d for d in loader_defs
        if d.source in (AGENT_SOURCE_ORG_DB, AGENT_SOURCE_USER_DB)
    ]

    merged: dict[str, Any] = {}
    for layer in (platform_defs, bucket_defs, db_defs):
        for d in layer:
            merged[d.name] = d
    return list(merged.values())


@router.get("/agents", response_model=AgentListResponse)
async def list_agents(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> AgentListResponse:
    """List available sub-agent types from all four layers.

    Returns lightweight summaries: name, description, source,
    category, model, max_iterations, policy_profile, enabled.
    """
    all_agents = await _merged_agent_catalog(request, tenant)

    summaries = [
        AgentSummary(
            name=a.name,
            description=a.description,
            source=normalize_source(a.source),
            category=a.category,
            model=a.model,
            max_iterations=a.max_iterations,
            policy_profile=a.policy_profile,
            enabled=a.enabled,
        )
        for a in all_agents
    ]
    summaries.sort(key=lambda s: (s.category or "", s.name))
    return AgentListResponse(agents=summaries, total=len(summaries))


@router.get("/agents/{name}", response_model=AgentDetail)
async def view_agent(
    name: str,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> AgentDetail:
    """Return the full sub-agent definition.

    The ``system_prompt`` field contains the AGENT.md body (everything
    after the YAML frontmatter).
    """
    all_agents = await _merged_agent_catalog(request, tenant)

    agent_def = next((a for a in all_agents if a.name == name), None)
    if agent_def is None:
        raise HTTPException(
            status_code=404, detail=f"Sub-agent '{name}' not found.",
        )

    return AgentDetail(
        name=agent_def.name,
        description=agent_def.description,
        source=normalize_source(agent_def.source),
        system_prompt=agent_def.system_prompt,
        tools=agent_def.tools,
        disallowed_tools=agent_def.disallowed_tools,
        model=agent_def.model,
        max_iterations=agent_def.max_iterations,
        policy_profile=agent_def.policy_profile,
        category=agent_def.category,
        tags=agent_def.tags,
        enabled=agent_def.enabled,
    )


@router.post(
    "/agents", response_model=AgentActionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent(
    body: CreateAgentRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> AgentActionResponse:
    """Create a new user-scoped sub-agent type."""
    raise_validation(_validate_agent_name(body.name))
    raise_validation(_validate_category(body.category))
    raise_validation(_validate_content(body.content))
    raise_validation(_validate_name_matches_frontmatter(body.name, body.content))

    ts = _get_tenant_storage(request, tenant)
    await ts.ensure_bucket()

    existing = await ts.agent_exists(body.name)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A sub-agent named '{body.name}' already exists.",
        )

    await ts.write_agent(body.name, body.content, body.category)
    return AgentActionResponse(
        success=True,
        message=f"Sub-agent '{body.name}' created.",
        category=body.category,
    )


@router.put("/agents/{name}", response_model=AgentActionResponse)
async def edit_agent(
    name: str,
    body: EditAgentRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> AgentActionResponse:
    """Replace the full AGENT.md content of an existing sub-agent."""
    raise_validation(_validate_content(body.content))
    raise_validation(_validate_name_matches_frontmatter(name, body.content))

    ts = _get_tenant_storage(request, tenant)
    existing = await ts.agent_exists(name)
    if not existing:
        raise HTTPException(
            status_code=404, detail=f"Sub-agent '{name}' not found.",
        )

    await ts.overwrite_agent(existing["key_prefix"], body.content)
    return AgentActionResponse(
        success=True, message=f"Sub-agent '{name}' updated.",
    )


@router.delete("/agents/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    name: str,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> None:
    """Delete a sub-agent from tenant storage."""
    ts = _get_tenant_storage(request, tenant)
    existing = await ts.agent_exists(name)
    if not existing:
        raise HTTPException(
            status_code=404, detail=f"Sub-agent '{name}' not found.",
        )
    await ts.delete_agent(existing["key_prefix"])
