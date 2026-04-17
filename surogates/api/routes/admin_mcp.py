"""Admin CRUD for MCP server registrations.

Writes land in the ``mcp_servers`` table.  The MCP proxy merges DB rows
with platform-volume configs (``/etc/surogates/mcp/``) — see
:mod:`surogates.mcp_proxy.loader`.

Routes mount under ``/v1/admin/mcp-servers``.  ``admin`` permission is
required for cross-org operations; users without ``admin`` may only
manage servers scoped to their own ``tenant.org_id`` / own
``tenant.user_id``.

Secrets must never be stored in ``env`` — use ``credential_refs`` to
reference entries in the encrypted credential vault.  The proxy
resolves refs at connection time and injects them into the MCP
server's env (stdio) or headers (http).
"""

from __future__ import annotations

import logging
import uuid
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from surogates.db.models import McpServer, Org, User
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext
from surogates.tools.loader import MCPTransport

from ._admin_scope import require_tenant_scope

logger = logging.getLogger(__name__)

router = APIRouter()


class CredentialRef(BaseModel):
    """How to resolve a vault entry and inject it into the MCP server.

    ``env``: inject value as this environment variable (stdio
    servers).  ``header``: inject as this HTTP header (http servers).
    ``prefix``: optional string prepended to the value (e.g. ``Bearer ``).
    Exactly one of ``env`` or ``header`` should be provided — if
    neither is set, the proxy falls back to defaults (stdio → env
    ``name``; http → ``Authorization: Bearer <value>``).
    """

    name: str = Field(..., min_length=1, max_length=128)
    env: str | None = None
    header: str | None = None
    prefix: str | None = None


class McpServerCreate(BaseModel):
    org_id: UUID
    user_id: UUID | None = None
    name: str = Field(..., min_length=1, max_length=128)
    transport: MCPTransport = MCPTransport.STDIO
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    credential_refs: list[CredentialRef] = Field(default_factory=list)
    timeout: int = Field(120, ge=1, le=3600)
    enabled: bool = True


class McpServerUpdate(BaseModel):
    transport: MCPTransport | None = None
    command: str | None = None
    args: list[str] | None = None
    url: str | None = None
    env: dict[str, str] | None = None
    credential_refs: list[CredentialRef] | None = None
    timeout: int | None = Field(None, ge=1, le=3600)
    enabled: bool | None = None


class McpServerResponse(BaseModel):
    id: UUID
    org_id: UUID
    user_id: UUID | None
    name: str
    transport: MCPTransport
    command: str | None
    args: list[str]
    url: str | None
    env: dict[str, str]
    credential_refs: list[CredentialRef]
    timeout: int
    enabled: bool


class McpServerListResponse(BaseModel):
    servers: list[McpServerResponse]
    total: int


def _assert_valid_transport_pair(
    transport: MCPTransport, command: str | None, url: str | None,
) -> None:
    """Enforce stdio↔command and http↔url pairing."""
    if transport == MCPTransport.STDIO and not command:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="stdio transport requires 'command'.",
        )
    if transport == MCPTransport.HTTP and not url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="http transport requires 'url'.",
        )


def _to_response(server: McpServer) -> McpServerResponse:
    refs = [
        CredentialRef(name=r) if isinstance(r, str) else CredentialRef(**r)
        for r in (server.credential_refs or [])
    ]
    return McpServerResponse(
        id=server.id,
        org_id=server.org_id,
        user_id=server.user_id,
        name=server.name,
        transport=MCPTransport(server.transport),
        command=server.command,
        args=list(server.args or []),
        url=server.url,
        env=dict(server.env or {}),
        credential_refs=refs,
        timeout=server.timeout,
        enabled=server.enabled,
    )


def _refs_to_json(refs: list[CredentialRef]) -> list[dict]:
    """Serialize credential refs for JSONB storage."""
    return [r.model_dump(exclude_none=True) for r in refs]


@router.post(
    "/mcp-servers",
    response_model=McpServerResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_mcp_server(
    body: McpServerCreate,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> McpServerResponse:
    """Register a new MCP server for an org or user."""
    require_tenant_scope(tenant, body.org_id, body.user_id, resource="MCP servers")
    _assert_valid_transport_pair(body.transport, body.command, body.url)

    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        org = await session.get(Org, body.org_id)
        if org is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Organisation {body.org_id} not found.",
            )

        if body.user_id is not None:
            user = await session.get(User, body.user_id)
            if user is None or user.org_id != body.org_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User {body.user_id} not found in org {body.org_id}.",
                )

        existing_q = select(McpServer).where(
            McpServer.org_id == body.org_id,
            McpServer.name == body.name,
        )
        if body.user_id is None:
            existing_q = existing_q.where(McpServer.user_id.is_(None))
        else:
            existing_q = existing_q.where(McpServer.user_id == body.user_id)

        if (await session.execute(existing_q)).scalar_one_or_none() is not None:
            scope = "user" if body.user_id else "org"
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"MCP server {body.name!r} already registered at {scope} scope.",
            )

        server = McpServer(
            id=uuid.uuid4(),
            org_id=body.org_id,
            user_id=body.user_id,
            name=body.name,
            transport=body.transport.value,
            command=body.command,
            args=body.args,
            url=body.url,
            env=body.env,
            credential_refs=_refs_to_json(body.credential_refs),
            timeout=body.timeout,
            enabled=body.enabled,
        )
        session.add(server)
        await session.commit()
        await session.refresh(server)

    return _to_response(server)


@router.get("/mcp-servers", response_model=McpServerListResponse)
async def list_mcp_servers(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    org_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> McpServerListResponse:
    """List MCP servers visible to the tenant.

    Non-admins see their own org's org-wide servers plus their own
    user-scoped servers.  Admins may filter by ``org_id`` or omit it
    to list across the whole platform.  ``total`` is the true
    unpaginated count.
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    session_factory = request.app.state.session_factory
    is_admin = "admin" in tenant.permissions

    base = select(McpServer)
    if is_admin:
        if org_id is not None:
            base = base.where(McpServer.org_id == org_id)
    else:
        base = base.where(McpServer.org_id == tenant.org_id).where(
            (McpServer.user_id.is_(None)) | (McpServer.user_id == tenant.user_id)
        )

    async with session_factory() as session:
        count_stmt = select(func.count()).select_from(base.subquery())
        total = (await session.execute(count_stmt)).scalar_one()

        page_stmt = base.order_by(McpServer.name.asc()).limit(limit).offset(offset)
        servers = (await session.execute(page_stmt)).scalars().all()

    return McpServerListResponse(
        servers=[_to_response(s) for s in servers],
        total=total,
    )


@router.get("/mcp-servers/{server_id}", response_model=McpServerResponse)
async def get_mcp_server(
    server_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> McpServerResponse:
    """Retrieve a single MCP server by ID."""
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        server = await session.get(McpServer, server_id)

    if server is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"MCP server {server_id} not found.",
        )

    require_tenant_scope(
        tenant, server.org_id, server.user_id, resource="MCP servers",
    )
    return _to_response(server)


@router.put("/mcp-servers/{server_id}", response_model=McpServerResponse)
async def update_mcp_server(
    server_id: UUID,
    body: McpServerUpdate,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> McpServerResponse:
    """Partially update an MCP server.  Omitted fields stay unchanged."""
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        server = await session.get(McpServer, server_id)
        if server is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"MCP server {server_id} not found.",
            )

        require_tenant_scope(
            tenant, server.org_id, server.user_id, resource="MCP servers",
        )

        updates = body.model_dump(exclude_unset=True)
        if "transport" in updates and updates["transport"] is not None:
            updates["transport"] = MCPTransport(updates["transport"]).value
        if "credential_refs" in updates and updates["credential_refs"] is not None:
            updates["credential_refs"] = _refs_to_json(
                [CredentialRef(**r) if isinstance(r, dict) else r
                 for r in updates["credential_refs"]]
            )

        for key, value in updates.items():
            setattr(server, key, value)

        _assert_valid_transport_pair(
            MCPTransport(server.transport), server.command, server.url,
        )

        await session.commit()
        await session.refresh(server)

    return _to_response(server)


@router.delete(
    "/mcp-servers/{server_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_mcp_server(
    server_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> None:
    """Remove an MCP server registration."""
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        server = await session.get(McpServer, server_id)
        if server is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"MCP server {server_id} not found.",
            )

        require_tenant_scope(
            tenant, server.org_id, server.user_id, resource="MCP servers",
        )

        await session.delete(server)
        await session.commit()
