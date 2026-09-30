"""Desktop devices: registration, listing, revocation and reauthorization.

Signed-in users manage their own devices here.  A device's own token is
accepted only by the device link.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, StringConstraints

from surogates.devices.store import DeviceRecord, DeviceStore, IssuedDevice
from surogates.runtime import AgentRuntimeContext, agent_runtime_context_dep
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

router = APIRouter(prefix="/devices")

AgentRuntime = Annotated[AgentRuntimeContext, Depends(agent_runtime_context_dep)]
Tenant = Annotated[TenantContext, Depends(get_current_tenant)]


class DeviceCreate(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class DeviceOut(BaseModel):
    id: UUID
    name: str
    token_prefix: str
    created_at: datetime
    last_seen_at: datetime | None
    revoked_at: datetime | None


class DeviceIssued(DeviceOut):
    token: str


def _owner(tenant: TenantContext, ctx: AgentRuntimeContext) -> dict:
    """The owner columns every device query is scoped to."""
    if tenant.user_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Devices belong to a user account.")
    if str(tenant.org_id) != ctx.org_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This agent belongs to another organization.")
    return {"org_id": tenant.org_id, "agent_id": ctx.agent_id, "user_id": tenant.user_id}


def _store(request: Request) -> DeviceStore:
    return DeviceStore(request.app.state.session_factory)


def _out(device: DeviceRecord) -> DeviceOut:
    return DeviceOut(
        id=device.id,
        name=device.name,
        token_prefix=device.token_prefix,
        created_at=device.created_at,
        last_seen_at=device.last_seen_at,
        revoked_at=device.revoked_at,
    )


def _issued(issued: IssuedDevice) -> DeviceIssued:
    return DeviceIssued(**_out(issued.device).model_dump(), token=issued.token)


@router.post("", response_model=DeviceIssued, status_code=status.HTTP_201_CREATED)
async def register_device(
    body: DeviceCreate, request: Request, ctx: AgentRuntime, tenant: Tenant,
) -> DeviceIssued:
    issued = await _store(request).create(name=body.name, **_owner(tenant, ctx))
    return _issued(issued)


@router.get("", response_model=list[DeviceOut])
async def list_devices(request: Request, ctx: AgentRuntime, tenant: Tenant) -> list[DeviceOut]:
    devices = await _store(request).list_for_user(**_owner(tenant, ctx))
    return [_out(device) for device in devices]


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_device(
    device_id: UUID, request: Request, ctx: AgentRuntime, tenant: Tenant,
) -> Response:
    device = await _store(request).revoke(device_id, **_owner(tenant, ctx))
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such device.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{device_id}/reauthorize", response_model=DeviceIssued)
async def reauthorize_device(
    device_id: UUID, request: Request, ctx: AgentRuntime, tenant: Tenant,
) -> DeviceIssued:
    issued = await _store(request).reauthorize(device_id, **_owner(tenant, ctx))
    if issued is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such device.")
    return _issued(issued)
