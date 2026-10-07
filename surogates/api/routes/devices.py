"""Desktop devices: registration, listing, revocation and reauthorization.

Signed-in users manage their own devices here.  A device's own token is
accepted only by the device link.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, status
from pydantic import BaseModel, StringConstraints

from surogates.api.routes._shared import require_recent_sign_in
from surogates.devices.link import serve_device_link
from surogates.devices.operations import DeviceOperations
from surogates.devices.presence import DevicePresence
from surogates.devices.store import DeviceRecord, DeviceStore, IssuedDevice
from surogates.runtime import AgentRuntimeContext, agent_runtime_context_dep
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.auth.oauth import OAuthTokens
from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)

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
    online: bool


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


def _presence(request: Request) -> DevicePresence:
    return DevicePresence(request.app.state.redis)


async def _notify(request: Request, device_id: UUID, message: str) -> None:
    """Tell the device's connection, best effort.

    The change is already committed, so failing the request now would hide it
    (for reauthorization, it would lose the only copy of the new token).  The
    link's periodic database check closes a connection whose message was lost.
    """
    try:
        await _presence(request).publish(device_id, message)
    except Exception:
        logger.warning("could not notify device %s", device_id, exc_info=True)


async def _bind_sign_in(request: Request, tenant: TenantContext, device_id: UUID) -> None:
    """Bind the desktop's sign-in that added or restored the computer to it: revoking it ends both."""
    if tenant.oauth_family_id is not None:
        await OAuthTokens(request.app.state.session_factory).bind(tenant.oauth_family_id, device_id)


def _out(device: DeviceRecord, *, online: bool) -> DeviceOut:
    return DeviceOut(
        id=device.id,
        name=device.name,
        token_prefix=device.token_prefix,
        created_at=device.created_at,
        last_seen_at=device.last_seen_at,
        revoked_at=device.revoked_at,
        online=online,
    )


def _issued(issued: IssuedDevice) -> DeviceIssued:
    # A token just issued has not connected yet.
    return DeviceIssued(**_out(issued.device, online=False).model_dump(), token=issued.token)


@router.post("", response_model=DeviceIssued, status_code=status.HTTP_201_CREATED)
async def register_device(
    body: DeviceCreate, request: Request, ctx: AgentRuntime, tenant: Tenant,
) -> DeviceIssued:
    owner = _owner(tenant, ctx)
    require_recent_sign_in(tenant)
    issued = await _store(request).create(name=body.name, **owner)
    await _bind_sign_in(request, tenant, issued.device.id)
    return _issued(issued)


@router.get("", response_model=list[DeviceOut])
async def list_devices(request: Request, ctx: AgentRuntime, tenant: Tenant) -> list[DeviceOut]:
    devices = await _store(request).list_for_user(**_owner(tenant, ctx))
    online = await _presence(request).online([device.id for device in devices])
    return [_out(device, online=device.id in online) for device in devices]


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_device(
    device_id: UUID, request: Request, ctx: AgentRuntime, tenant: Tenant,
) -> Response:
    device = await _store(request).revoke(device_id, **_owner(tenant, ctx))
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such device.")
    await _notify(request, device_id, f"revoked:{device.credential_generation}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{device_id}/reauthorize", response_model=DeviceIssued)
async def reauthorize_device(
    device_id: UUID, request: Request, ctx: AgentRuntime, tenant: Tenant,
) -> DeviceIssued:
    owner = _owner(tenant, ctx)
    require_recent_sign_in(tenant)
    issued = await _store(request).reauthorize(device_id, **owner)
    if issued is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such device.")
    await _bind_sign_in(request, tenant, device_id)
    await _notify(request, device_id, f"rotated:{issued.device.credential_generation}")
    return _issued(issued)


@router.websocket("/connect")
async def device_link(websocket: WebSocket) -> None:
    """The desktop's device link.  Authenticated by the device token, not by a user."""
    await serve_device_link(
        websocket,
        store=DeviceStore(websocket.app.state.session_factory),
        presence=DevicePresence(websocket.app.state.redis),
        operations=DeviceOperations(
            websocket.app.state.session_factory, websocket.app.state.redis,
        ),
    )
