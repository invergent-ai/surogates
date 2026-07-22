"""End-user commerce routes for the agent web app.

The Plan & tokens tab in the web app's settings shows what the agent
sells and what the signed-in user currently holds, and lets them buy.
The harness fronts surogate-ops with its runtime token, keyed to the
authenticated user's project-Firebase identity — the browser never
handles ops credentials or cross-origin calls.

Users without a Firebase identity (operator-provisioned database /
external accounts) get the offer list with ``purchasable=false`` —
they chat unmetered by the operator's choice and have no buyer
identity a purchase could attach to.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from surogates.runtime import AgentRuntimeContext, agent_runtime_context_dep
from surogates.runtime.platform_client import PlatformAuthError
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter()


class CommerceCheckoutRequest(BaseModel):
    offer_id: str = Field(min_length=1, max_length=36)


class CommerceCheckoutResponse(BaseModel):
    url: str


async def _firebase_identity(
    request: Request, tenant: TenantContext,
) -> tuple[str, str | None, str | None] | None:
    """(firebase_uid, email, name) for the signed-in user, or ``None``
    when the principal has no project-Firebase identity."""
    if tenant.user_id is None:
        return None
    from surogates.db.models import User

    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        user = await db.scalar(
            select(User).where(
                User.id == tenant.user_id,
                User.org_id == tenant.org_id,
            )
        )
    if (
        user is None
        or not (user.auth_provider or "").startswith("firebase:")
        or not user.external_id
    ):
        return None
    return user.external_id, user.email, user.display_name


def _platform_client(request: Request):
    client = getattr(request.app.state, "platform_client", None)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Platform connection is not available.",
        )
    return client


@router.get("/commerce/overview")
async def commerce_overview(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    agent_runtime: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> dict:
    """Offers + the caller's entitlement, shaped for the settings tab.

    ``purchasable`` tells the tab whether Buy buttons can work for
    this principal (it requires a Firebase identity to meter against).
    """
    identity = await _firebase_identity(request, tenant)
    client = _platform_client(request)
    if identity is None:
        # Still show what the agent sells — with buying disabled. No
        # identity is sent, so ops mints no buyer/entitlement row.
        try:
            summary = await client.commerce_summary(
                str(agent_runtime.agent_id),
            )
        except (PlatformAuthError, httpx.HTTPError):
            logger.warning("commerce summary unavailable", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Plan information is temporarily unavailable.",
            )
        summary["entitlement"] = None
        summary["purchasable"] = False
        return summary
    uid, email, name = identity
    try:
        summary = await client.commerce_summary(
            str(agent_runtime.agent_id),
            firebase_uid=uid,
            email=email,
            name=name,
        )
    except (PlatformAuthError, httpx.HTTPError):
        logger.warning("commerce summary unavailable", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Plan information is temporarily unavailable.",
        )
    summary["purchasable"] = summary.get("mode") != "free"
    return summary


@router.post("/commerce/checkout", response_model=CommerceCheckoutResponse)
async def commerce_checkout(
    body: CommerceCheckoutRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    agent_runtime: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> CommerceCheckoutResponse:
    identity = await _firebase_identity(request, tenant)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Purchases require an account created through the "
                "agent's sign-in (your account was provisioned by the "
                "operator)."
            ),
        )
    uid, email, name = identity
    client = _platform_client(request)
    try:
        payload = await client.commerce_checkout(
            str(agent_runtime.agent_id),
            firebase_uid=uid,
            offer_id=body.offer_id,
            email=email,
            name=name,
        )
    except httpx.HTTPStatusError as exc:
        detail = "Checkout is unavailable for this offer."
        try:
            detail = str(exc.response.json().get("detail") or detail)
        except ValueError:
            pass
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=detail,
        ) from exc
    except (PlatformAuthError, httpx.HTTPError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Checkout is temporarily unavailable; try again.",
        ) from exc
    return CommerceCheckoutResponse(url=payload["url"])
