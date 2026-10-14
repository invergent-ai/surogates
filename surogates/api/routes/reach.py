"""Browser push: a signed-in web user allows (or stops) notifications from this agent
in one browser (:mod:`surogates.channels.web_push`)."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from surogates.runtime import AgentRuntimeContext, agent_runtime_context_dep, rate_limit_dep
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Browser push
# ---------------------------------------------------------------------------


class PushKeys(BaseModel):
    p256dh: str = Field(..., min_length=40, max_length=200)
    auth: str = Field(..., min_length=10, max_length=100)


class PushSubscriptionBody(BaseModel):
    """``PushSubscription.toJSON()`` as the browser gives it."""

    endpoint: str = Field(..., max_length=2000)
    keys: PushKeys


class PushEndpointBody(BaseModel):
    endpoint: str = Field(..., max_length=2000)


def _user(tenant: TenantContext) -> UUID:
    if tenant.user_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a signed-in person can allow notifications.")
    return tenant.user_id


@router.get("/push/key")
async def push_key(request: Request) -> dict:
    """The VAPID public key a browser subscribes with; ``null`` when push is off."""
    return {"public_key": request.app.state.settings.vapid.public_key or None}


@router.post("/push/subscriptions", status_code=status.HTTP_204_NO_CONTENT)
async def push_subscribe(
    body: PushSubscriptionBody,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    agent_runtime: AgentRuntimeContext = Depends(agent_runtime_context_dep),
    _rate: None = Depends(rate_limit_dep),
) -> Response:
    from surogates.channels.web_push import endpoint_problem, keys_problem, subscribe

    problem = await endpoint_problem(
        body.endpoint, request.app.state.settings.vapid.extra_push_hosts,
    ) or keys_problem(body.keys.p256dh, body.keys.auth)
    if problem:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, problem)
    await subscribe(request.app.state.session_factory, org_id=tenant.org_id, agent_id=agent_runtime.agent_id,
                    user_id=_user(tenant), endpoint=body.endpoint, p256dh=body.keys.p256dh, auth=body.keys.auth)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/push/subscriptions", status_code=status.HTTP_204_NO_CONTENT)
async def push_unsubscribe(
    body: PushEndpointBody,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> Response:
    from surogates.channels.web_push import unsubscribe

    await unsubscribe(request.app.state.session_factory, user_id=_user(tenant), endpoint=body.endpoint)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
