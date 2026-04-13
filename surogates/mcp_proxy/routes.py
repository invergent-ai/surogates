"""MCP proxy API endpoints.

Two endpoints that sandbox pods call:

- ``POST /mcp/v1/tools/list`` — discover available MCP tools for a session
- ``POST /mcp/v1/tools/call`` — execute an MCP tool call with credential injection
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from surogates.mcp_proxy.auth import ProxyAuthContext, get_proxy_auth
from surogates.mcp_proxy.loader import load_mcp_configs
from surogates.mcp_proxy.pool import ConnectionPool
from surogates.tools.mcp.client import _sanitize_error

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class ToolCallRequest(BaseModel):
    """Request body for ``POST /mcp/v1/tools/call``."""

    name: str
    arguments: dict[str, Any] = {}


class ToolCallResponse(BaseModel):
    """Response body for ``POST /mcp/v1/tools/call``."""

    result: str | None = None
    error: str | None = None


class ToolSchema(BaseModel):
    """A single tool schema in the list response."""

    name: str
    description: str
    parameters: dict[str, Any]


class ToolListResponse(BaseModel):
    """Response body for ``POST /mcp/v1/tools/list``."""

    tools: list[ToolSchema]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _ensure_tenant_connected(
    pool: ConnectionPool,
    auth: ProxyAuthContext,
    request: Request,
) -> list[dict[str, Any]]:
    """Load MCP configs and ensure the tenant is connected.

    Returns cached schemas if the tenant is already connected, otherwise
    loads configs from DB + platform and connects.
    """
    cached = pool.get_cached_schemas(auth.org_id, auth.user_id)
    if cached is not None:
        return cached

    configs = await load_mcp_configs(
        org_id=auth.org_id,
        user_id=auth.user_id,
        session_factory=request.app.state.session_factory,
        vault=request.app.state.vault,
        platform_mcp_dir=request.app.state.platform_mcp_dir,
    )

    if not configs:
        return []

    return await pool.ensure_connected(
        org_id=auth.org_id,
        user_id=auth.user_id,
        configs=configs,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/mcp/v1/tools/list", response_model=ToolListResponse)
async def list_tools(
    request: Request,
    auth: ProxyAuthContext = Depends(get_proxy_auth),
) -> ToolListResponse:
    """Discover available MCP tools for the authenticated tenant."""
    pool: ConnectionPool = request.app.state.pool
    schemas = await _ensure_tenant_connected(pool, auth, request)

    return ToolListResponse(
        tools=[
            ToolSchema(
                name=s.get("name", ""),
                description=s.get("description", ""),
                parameters=s.get("parameters", {}),
            )
            for s in schemas
        ]
    )


@router.post("/mcp/v1/tools/call", response_model=ToolCallResponse)
async def call_tool(
    body: ToolCallRequest,
    request: Request,
    auth: ProxyAuthContext = Depends(get_proxy_auth),
) -> ToolCallResponse:
    """Execute an MCP tool call with credential injection."""
    pool: ConnectionPool = request.app.state.pool

    # Lazy-connect on first call for this tenant.
    schemas = await _ensure_tenant_connected(pool, auth, request)
    if not schemas:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No MCP servers configured for this tenant.",
        )

    result = await pool.call_tool(
        org_id=auth.org_id,
        user_id=auth.user_id,
        tool_name=body.name,
        arguments=body.arguments,
    )

    # Parse the result to separate error from success.
    try:
        parsed = json.loads(result)
        if isinstance(parsed, dict) and "error" in parsed:
            return ToolCallResponse(
                error=_sanitize_error(str(parsed["error"])),
            )
    except (json.JSONDecodeError, TypeError):
        pass

    return ToolCallResponse(result=_sanitize_error(result))
