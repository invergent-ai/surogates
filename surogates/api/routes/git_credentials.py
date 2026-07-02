"""Store an agent/user GitHub fine-grained PAT in the encrypted vault.

Keyed on the caller's principal (end user, or a service account — the agent's
own SA when ops forwards as it), stored under the fixed name ``git_pat`` so a
coding run resolves it under the same principal.  Plaintext never returned.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from surogates.api.routes.coding_agents import _require_principal, _vault
from surogates.runtime import AgentRuntimeContext, agent_runtime_context_dep
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

router = APIRouter()

GIT_PAT_NAME = "git_pat"


def _validate_pat(value: str) -> str:
    v = (value or "").strip()
    if not v.startswith("github_pat_"):
        raise ValueError("Expected a GitHub fine-grained token (github_pat_…).")
    return v


class PatSubmit(BaseModel):
    value: str = Field(..., repr=False)


@router.post("/git-credentials/credential")
async def submit_pat(
    body: PatSubmit,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    ctx: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> dict:
    principal = _require_principal(tenant, ctx)
    try:
        pat = _validate_pat(body.value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _vault(request).store(tenant.org_id, GIT_PAT_NAME, pat, **principal)
    return {"connected": True}


@router.get("/git-credentials/connection")
async def pat_status(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    ctx: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> dict:
    principal = _require_principal(tenant, ctx)
    value = await _vault(request).retrieve(tenant.org_id, GIT_PAT_NAME, **principal)
    return {"connected": value is not None}


@router.delete(
    "/git-credentials/credential", status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_pat(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    ctx: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> Response:
    principal = _require_principal(tenant, ctx)
    await _vault(request).delete(tenant.org_id, GIT_PAT_NAME, **principal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
