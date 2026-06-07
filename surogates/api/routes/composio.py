"""End-user Composio connect routes (agent-UI path).

The agent-chat-react ``ConnectionsPanel`` calls these as the authenticated
end-user (``TenantContext``).  They resolve the current agent via
``agent_runtime_context_dep`` and bridge to surogate-ops over the
``PlatformClient`` (runtime token); ops holds the Composio broker + API key.
The end-user's ``user_id`` is the same id the MCP proxy mints under, so
Composio resolves the matching connected account.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from surogates.runtime import AgentRuntimeContext, agent_runtime_context_dep
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

router = APIRouter()


def _require_end_user(tenant: TenantContext, ctx: AgentRuntimeContext) -> str:
    """Return the end-user id, rejecting service-account / cross-tenant callers.

    Connecting a Composio account is a human-user action; service-account
    sessions have no personal connection to make.  The org check stops a
    ``?agent_id=`` dev override from crossing project/org boundaries.
    """
    if tenant.user_id is None:
        raise HTTPException(status_code=401, detail="end-user identity required")
    if str(tenant.org_id) != ctx.org_id:
        raise HTTPException(status_code=403, detail="agent does not belong to this tenant")
    return str(tenant.user_id)


@router.get("/composio/connections")
async def list_composio_connections(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    ctx: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> dict:
    user_id = _require_end_user(tenant, ctx)
    pc = request.app.state.platform_client
    return await pc.composio_connections(ctx.agent_id, user_id)


@router.post("/composio/toolkits/{toolkit}/authorize")
async def authorize_composio_toolkit(
    toolkit: str,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    ctx: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> dict:
    user_id = _require_end_user(tenant, ctx)
    pc = request.app.state.platform_client
    try:
        return await pc.composio_authorize(ctx.agent_id, user_id, toolkit)
    except httpx.HTTPStatusError as exc:
        detail = "composio authorize failed"
        try:
            detail = exc.response.json().get("detail", detail)
        except Exception:  # noqa: BLE001 — best-effort detail passthrough
            pass
        raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
