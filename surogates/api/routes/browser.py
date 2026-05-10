"""Browser live-view and control endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

router = APIRouter()


class BrowserStateResponse(BaseModel):
    status: str
    control_owner: str | None
    live_view_path: str


def _route_prefix(request: Request) -> str:
    return "/v1/api" if request.url.path.startswith("/v1/api/") else "/v1"


@router.get(
    "/api/sessions/{session_id}/browser/state",
    response_model=BrowserStateResponse,
)
@router.get(
    "/sessions/{session_id}/browser/state",
    response_model=BrowserStateResponse,
)
async def get_browser_state(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> BrowserStateResponse:
    resolver = request.app.state.browser_resolver
    control = request.app.state.browser_control

    resolved = await resolver.resolve(
        str(session_id),
        expected_org_id=str(tenant.org_id),
    )
    if resolved is None:
        raise HTTPException(status_code=404, detail="No browser for session")

    holder = await control.held_by(str(session_id))
    return BrowserStateResponse(
        status="user-control" if holder else "live",
        control_owner=holder,
        live_view_path=(
            f"{_route_prefix(request)}/sessions/{session_id}/browser/live/"
        ),
    )
