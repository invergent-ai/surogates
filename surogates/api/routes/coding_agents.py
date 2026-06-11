"""End-user routes to connect coding-agent plans (capture model A).

The chat UI submits the credential the user pasted (a `claude setup-token`,
a Codex `auth.json`, or an API key); we validate and store it user-scoped
in the encrypted vault.  Plaintext is never returned by any route.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from surogates.coding_agents.credentials import (
    PROVIDERS,
    CodingAgentCredentials,
    CredentialError,
    validate_pasted,
)
from surogates.runtime import AgentRuntimeContext, agent_runtime_context_dep
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext
from surogates.tenant.credentials import CredentialVault

router = APIRouter()


class CredentialSubmit(BaseModel):
    mode: str = Field(..., description="'oauth' or 'api_key'")
    value: str = Field(..., repr=False)


def _require_principal(
    tenant: TenantContext, ctx: AgentRuntimeContext,
) -> dict[str, UUID | None]:
    """Resolve the connecting principal — an end user (agent UI) or a
    service account (the ops-chat SA the Studio work surface runs under).

    Returns the principal kwargs for ``CodingAgentCredentials`` so the plan
    is stored and resolved under whoever actually runs ``/code``.
    """
    if tenant.user_id is None and tenant.service_account_id is None:
        raise HTTPException(
            status_code=401, detail="user or service-account identity required",
        )
    if str(tenant.org_id) != ctx.org_id:
        raise HTTPException(
            status_code=403, detail="agent does not belong to this tenant",
        )
    return {
        "user_id": tenant.user_id,
        "service_account_id": tenant.service_account_id,
    }


def _vault(request: Request) -> CredentialVault:
    vault = getattr(request.app.state, "credential_vault", None)
    if vault is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Credential vault is not configured. Set SUROGATES_ENCRYPTION_KEY.",
        )
    return vault


@router.get("/coding-agents/connections")
async def list_connections(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    ctx: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> dict:
    principal = _require_principal(tenant, ctx)
    creds = CodingAgentCredentials(_vault(request))
    connections = await creds.statuses(org_id=tenant.org_id, **principal)
    return {"connections": connections}


@router.post("/coding-agents/{provider}/credential")
async def submit_credential(
    provider: str,
    body: CredentialSubmit,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    ctx: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> dict:
    principal = _require_principal(tenant, ctx)
    try:
        bundle = validate_pasted(provider, body.mode, body.value)
    except CredentialError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    creds = CodingAgentCredentials(_vault(request))
    await creds.store(org_id=tenant.org_id, bundle=bundle, **principal)
    return {"provider": provider, "connected": True, "auth_mode": bundle.auth_mode}


@router.delete(
    "/coding-agents/{provider}", status_code=status.HTTP_204_NO_CONTENT,
)
async def disconnect(
    provider: str,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    ctx: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> Response:
    principal = _require_principal(tenant, ctx)
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown provider {provider!r}.")
    creds = CodingAgentCredentials(_vault(request))
    removed = await creds.delete(
        org_id=tenant.org_id, provider=provider, **principal,
    )
    if not removed:
        raise HTTPException(status_code=404, detail=f"{provider} is not connected.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
