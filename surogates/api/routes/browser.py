"""Browser live-view and control endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any
from uuid import UUID

import httpx
import websockets
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi import WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel

from surogates.browser.cdp import CdpClient
from surogates.browser.client import KernelBrowserClient
from surogates.browser.control import AcquireOutcome
from surogates.browser.shell import ShellSession
from surogates.session.events import EventType
from surogates.tenant.auth.middleware import (
    authenticate_websocket_tenant,
    get_current_tenant,
)
from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter()


class BrowserStateResponse(BaseModel):
    status: str
    control_owner: str | None
    live_view_path: str


class BrowserControlRequest(BaseModel):
    action: str
    owner_user_id: str | None = None


def _route_prefix(request: Request) -> str:
    return "/v1/api" if request.url.path.startswith("/v1/api/") else "/v1"


def _browser_preview_client(rest_url: str) -> KernelBrowserClient:
    return KernelBrowserClient(rest_url)


# Screencast frames arrive base64-encoded, so a capped 74 KB JPEG crosses the
# CDP socket at ~99 KB. Bounded well above that, and well below "unbounded".
MAX_CDP_FRAME = 32 * 1024 * 1024


# How long to let a freshly provisioned browser finish opening its debug port.
# The backend's readiness check polls the kernel REST API on :10001, and Chrome
# binds :9222 after that, so a viewer who opens the pane during provisioning
# arrives before CDP is listening. Bounded: a browser that is genuinely gone
# must not hold a viewer's socket open indefinitely.
CDP_READY_TIMEOUT = 20.0
CDP_POLL_INTERVAL = 0.25


async def _poll_cdp_version(
    http: httpx.AsyncClient,
    base: str,
    timeout: float,
) -> str:
    """Read ``/json/version``, waiting out a port that is not open yet.

    Only transport errors are retried. A reachable endpoint answering the
    wrong shape is a broken browser rather than a slow one, and retrying
    would delay a failure that will not fix itself.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            response = await http.get(f"{base}/json/version")
            return response.json()["webSocketDebuggerUrl"]
        except (httpx.TransportError, httpx.HTTPStatusError):
            if loop.time() >= deadline:
                raise
            await asyncio.sleep(CDP_POLL_INTERVAL)


async def _cdp_browser_ws_url(
    cdp_url: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = CDP_READY_TIMEOUT,
) -> str:
    """Resolve the pod's browser-level DevTools socket from its CDP endpoint.

    The URL in the registry is the port, not the socket: Chrome mints a fresh
    ``/devtools/browser/<uuid>`` path per launch, so it has to be read from
    ``/json/version`` rather than assumed.
    """

    base = cdp_url.replace("ws://", "http://", 1).replace(
        "wss://", "https://", 1
    ).rstrip("/")
    if client is not None:
        return await _poll_cdp_version(client, base, timeout)
    async with httpx.AsyncClient(timeout=10.0) as owned:
        return await _poll_cdp_version(owned, base, timeout)


def _effective_live_view_user(
    *,
    tenant: TenantContext,
    path: str,
    query_params: Any,
) -> str | None:
    if tenant.user_id is not None:
        return str(tenant.user_id)
    # Service-account-auth'd callers (the ops proxy, on /v1/api/*) carry
    # no per-user JWT, so they assert the effective user via
    # ``?owner_user_id=``.  The agent has already trusted the caller via
    # the bearer token at this point — same trust model as the
    # ``owner_user_id`` JSON field on POST /browser/control.
    if path.startswith("/v1/api/"):
        candidate = query_params.get("owner_user_id")
        if candidate:
            return str(candidate)
    return None


async def _require_session_agent(
    app_state: Any, session_id: UUID, tenant: TenantContext,
) -> None:
    """404 a browser session belonging to an agent this token is not bound to.

    These routes authorise on the session's ORG alone, so without this a
    customer API key minted for one agent can take control of, screenshot and
    tear down a SIBLING agent's live browser in the same org — the operator's
    other agents driving their own logged-in sessions.

    Checked against the session row's own ``agent_id``, never a
    request-supplied one. 404 rather than 403, matching the resolver's
    convention that a stranger cannot tell "exists but not yours" from
    "does not exist". Org-scoped control-plane tokens are untouched.

    Takes app state rather than a ``Request`` so the live-view WebSocket
    handler — which has a ``WebSocket``, not a request — is covered by the
    same guard as the HTTP routes.
    """
    bound = getattr(tenant, "service_account_agent_id", None)
    if bound is None:
        return
    store = getattr(app_state, "session_store", None)
    if store is None:
        return
    try:
        session = await store.get_session(session_id)
    except Exception:
        raise HTTPException(status_code=404, detail="No browser for session")
    if session.agent_id and session.agent_id != bound:
        raise HTTPException(status_code=404, detail="No browser for session")


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

    await _require_session_agent(request.app.state, session_id, tenant)
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


@router.post("/api/sessions/{session_id}/browser/control")
@router.post("/sessions/{session_id}/browser/control")
async def post_browser_control(
    session_id: UUID,
    body: BrowserControlRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict[str, str]:
    if body.action not in {"acquire", "release"}:
        raise HTTPException(
            status_code=400,
            detail="action must be 'acquire' or 'release'",
        )

    resolver = request.app.state.browser_resolver
    control = request.app.state.browser_control
    emit = getattr(request.app.state, "session_event_emitter", None)
    wake = getattr(request.app.state, "session_wake", None)
    if emit is None or wake is None:
        raise HTTPException(
            status_code=503,
            detail="Browser control dependencies are not available.",
        )

    await _require_session_agent(request.app.state, session_id, tenant)
    resolved = await resolver.resolve(
        str(session_id),
        expected_org_id=str(tenant.org_id),
    )
    if resolved is None:
        raise HTTPException(status_code=404, detail="No browser for session")

    owner_user_id = body.owner_user_id if _route_prefix(request) == "/v1/api" else None
    if owner_user_id is None and tenant.user_id is not None:
        owner_user_id = str(tenant.user_id)
    if owner_user_id is None:
        raise HTTPException(
            status_code=403,
            detail="Browser control requires a user identity.",
        )

    if body.action == "acquire":
        outcome, entry = await control.acquire(str(session_id), owner_user_id)
        if outcome == AcquireOutcome.GRANTED:
            await emit(
                str(session_id),
                EventType.BROWSER_CONTROL_GRANTED,
                {"session_id": str(session_id), "owner_user_id": entry.owner_user_id},
            )
            return {"outcome": "granted", "owner_user_id": entry.owner_user_id}
        if outcome == AcquireOutcome.REFRESHED:
            return {"outcome": "refreshed", "owner_user_id": entry.owner_user_id}
        raise HTTPException(
            status_code=409,
            detail={
                "outcome": "conflict",
                "holder_user_id": entry.owner_user_id,
                "acquired_at": entry.acquired_at.isoformat(),
            },
        )

    released = await control.release(str(session_id), owner_user_id)
    if not released:
        raise HTTPException(status_code=403, detail="not the holder")
    await emit(
        str(session_id),
        EventType.BROWSER_CONTROL_RETURNED,
        {"session_id": str(session_id), "released_by": owner_user_id},
    )
    await wake(str(session_id))
    return {"outcome": "released"}


@router.delete("/api/sessions/{session_id}/browser")
@router.delete("/sessions/{session_id}/browser")
async def delete_session_browser(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> Response:
    """Destroy the browser sandbox for a session.

    Idempotent: 204 whether or not a browser was attached. The pool,
    backend (when it exposes ``destroy_for_session``), and registry
    are all cleaned up — matching the cleanup performed when a session
    is deleted (see ``_destroy_deleted_session_browser`` in
    ``api.routes.sessions``).

    Tenant scope is enforced by resolving the browser first: if a
    registry entry exists, its ``org_id`` must match the caller's
    tenant. A 404 is returned for sessions in a different org so the
    endpoint never reveals foreign session ids.
    """
    resolver = request.app.state.browser_resolver
    await _require_session_agent(request.app.state, session_id, tenant)
    resolved = await resolver.resolve(
        str(session_id),
        expected_org_id=str(tenant.org_id),
    )
    if resolved is None:
        # No browser to close, OR the browser belongs to a different
        # org (resolver returns None in both cases). Either way, the
        # appropriate response is "nothing here" — 204 keeps the
        # idempotency contract intact.
        return Response(status_code=204)

    session_id_str = str(session_id)
    browser_pool = getattr(request.app.state, "browser_pool", None)
    if browser_pool is not None:
        try:
            await browser_pool.destroy_for_session(session_id_str)
        except Exception:
            logger.warning(
                "Failed to destroy browser pool entry for session %s",
                session_id,
                exc_info=True,
            )

    browser_backend = getattr(request.app.state, "browser_backend", None)
    if browser_backend is not None and hasattr(
        browser_backend, "destroy_for_session",
    ):
        try:
            await browser_backend.destroy_for_session(session_id_str)
        except Exception:
            logger.warning(
                "Failed to destroy backend browser resources for session %s",
                session_id,
                exc_info=True,
            )

    browser_registry = getattr(request.app.state, "browser_registry", None)
    if browser_registry is not None:
        try:
            await browser_registry.delete(session_id_str)
        except Exception:
            logger.warning(
                "Failed to delete browser registry entry for session %s",
                session_id,
                exc_info=True,
            )

    return Response(status_code=204)


@router.get("/api/sessions/{session_id}/browser/preview.png")
@router.get("/sessions/{session_id}/browser/preview.png")
async def get_browser_preview(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> Response:
    resolver = request.app.state.browser_resolver
    await _require_session_agent(request.app.state, session_id, tenant)
    resolved = await resolver.resolve(
        str(session_id),
        expected_org_id=str(tenant.org_id),
    )
    if resolved is None:
        raise HTTPException(status_code=404, detail="No browser for session")

    try:
        async with _browser_preview_client(resolved.endpoint.rest_url) as client:
            screenshot = await client.screenshot()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="Browser preview is unreachable.",
        ) from exc

    return Response(
        content=screenshot["png_bytes"],
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@router.websocket("/api/sessions/{session_id}/browser/shell")
@router.websocket("/sessions/{session_id}/browser/shell")
async def browser_shell_ws(websocket: WebSocket, session_id: UUID) -> None:
    """Stream one tab to a viewer, and carry their commands back.

    Unlike ``proxy_live_view_ws``, holding the control lease is NOT required to
    connect: frames always flow and only the command half is gated, so a viewer
    watches live instead of falling back to a still preview. The lease is
    re-checked per message rather than at connect time, so it expiring
    mid-session quietly turns the viewer into a spectator.
    """

    try:
        tenant = await authenticate_websocket_tenant(
            websocket.app,
            path=websocket.url.path,
            token=websocket.query_params.get("token"),
            cookies=websocket.cookies,
            authorization=websocket.headers.get("authorization"),
        )
    except HTTPException:
        # Starlette turns every close-before-accept into HTTP 403, so the
        # client cannot tell these apart. Log which one fired.
        logger.warning("browser shell rejected: unauthenticated")
        await websocket.close(code=4401, reason="unauthenticated")
        return

    resolver = websocket.app.state.browser_resolver
    control = websocket.app.state.browser_control
    try:
        await _require_session_agent(websocket.app.state, session_id, tenant)
    except HTTPException:
        logger.warning(
            "browser shell rejected: session %s is not this token's agent",
            session_id,
        )
        await websocket.close(code=4404, reason="no browser")
        return
    resolved = await resolver.resolve(
        str(session_id),
        expected_org_id=str(tenant.org_id),
    )
    if resolved is None:
        logger.warning(
            "browser shell rejected: no browser registered for session %s in org %s",
            session_id,
            tenant.org_id,
        )
        await websocket.close(code=4404, reason="no browser")
        return

    effective = _effective_live_view_user(
        tenant=tenant,
        path=websocket.url.path,
        query_params=websocket.query_params,
    )

    async def lease_held() -> bool:
        # Keyed on ``effective`` rather than ``tenant.user_id``, which is None
        # for the ops proxy's service-account connection and would make every
        # viewer a spectator.
        return (
            effective is not None
            and await control.held_by(str(session_id)) == effective
        )

    try:
        upstream_url = await _cdp_browser_ws_url(resolved.endpoint.cdp_url)
        upstream = await websockets.connect(upstream_url, max_size=MAX_CDP_FRAME)
    except Exception:
        logger.warning(
            "browser shell rejected: cannot reach CDP at %s",
            resolved.endpoint.cdp_url,
            exc_info=True,
        )
        await websocket.close(code=4502, reason="upstream unavailable")
        return

    await websocket.accept()
    session: ShellSession | None = None
    try:
        async with CdpClient(upstream) as cdp:
            session = ShellSession(cdp, websocket, lease_held=lease_held)
            await session.start()
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                raw = message.get("text")
                if raw is None:
                    # The client half of this protocol is JSON only; binary is
                    # the server's direction.
                    continue
                await session.handle(raw)
    except WebSocketDisconnect:
        return
    except Exception:
        logger.exception("browser shell session failed")
        with contextlib.suppress(Exception):
            await websocket.close(code=4500, reason="shell failed")
    finally:
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()
        with contextlib.suppress(Exception):
            await websocket.close()
