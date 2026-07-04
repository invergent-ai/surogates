"""Store/list/delete a managed agent's named SSH private keys in the vault.

Keyed on the caller's principal (end user, or a service account — the agent's
own SA when ops forwards as it), stored under ``ssh_key:<name>`` so a session
resolves it under the same principal.  Plaintext never returned.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from surogates.api.routes.coding_agents import _require_principal, _vault
from surogates.runtime import AgentRuntimeContext, agent_runtime_context_dep
from surogates.ssh_access.bundle import SSHKeyBundle, validate_private_key
from surogates.ssh_access.resolve import SSH_KEY_PREFIX
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

router = APIRouter()


class SSHKeySubmit(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    private_key: str = Field(..., repr=False)
    passphrase: str | None = Field(default=None, repr=False)


# A key name becomes part of a vault ref and of the sidecar shell script
# (``id_<name>``/``pass_<name>``); restrict it to a conservative charset so a
# metacharacter cannot inject into either.
_VALID_KEY_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def _vault_name(name: str) -> str:
    n = (name or "").strip()
    if not _VALID_KEY_NAME.match(n):
        raise HTTPException(status_code=422, detail="invalid key name")
    return f"{SSH_KEY_PREFIX}{n}"


@router.post("/ssh-credentials/credential")
async def submit_ssh_key(
    body: SSHKeySubmit,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    ctx: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> dict:
    principal = _require_principal(tenant, ctx)
    vault_name = _vault_name(body.name)
    try:
        key = validate_private_key(body.private_key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    bundle = SSHKeyBundle(private_key=key, passphrase=body.passphrase or None)
    await _vault(request).store(tenant.org_id, vault_name, bundle.to_json(), **principal)
    return {"connected": True, "name": body.name.strip()}


@router.get("/ssh-credentials/credentials")
async def list_ssh_keys(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    ctx: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> dict:
    principal = _require_principal(tenant, ctx)
    names = await _vault(request).list_names(tenant.org_id, **principal)
    creds = [
        {"name": n[len(SSH_KEY_PREFIX):]}
        for n in names
        if n.startswith(SSH_KEY_PREFIX)
    ]
    return {"credentials": creds}


@router.delete(
    "/ssh-credentials/credential", status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_ssh_key(
    request: Request,
    name: str = Query(..., min_length=1),
    tenant: TenantContext = Depends(get_current_tenant),
    ctx: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> Response:
    principal = _require_principal(tenant, ctx)
    await _vault(request).delete(tenant.org_id, _vault_name(name), **principal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
