"""Browser live-view and control endpoints."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import httpx
import websockets
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi import WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel

from surogates.browser.client import KernelBrowserClient
from surogates.browser.control import AcquireOutcome
from surogates.browser.rfb import is_input_frame
from surogates.session.events import EventType
from surogates.tenant.auth.middleware import (
    LIVE_VIEW_TOKEN_COOKIE,
    authenticate_websocket_tenant,
    get_current_tenant,
)
from surogates.tenant.context import TenantContext

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


async def _proxy_live_view_request(method: str, url: str, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(timeout=30.0) as client:
        return await client.request(method, url, **kwargs)


def _browser_preview_client(rest_url: str) -> KernelBrowserClient:
    return KernelBrowserClient(rest_url)


async def _connect_live_view_ws(url: str):
    return await websockets.connect(url, subprotocols=["binary"])


def _live_view_client_payload(message: dict[str, Any]) -> str | bytes | None:
    if message.get("bytes") is not None:
        return message["bytes"]
    if message.get("text") is not None:
        return message["text"]
    return None


async def _send_live_view_frame_to_client(
    websocket: WebSocket,
    frame: str | bytes,
) -> None:
    if isinstance(frame, str):
        await websocket.send_text(frame)
        return
    await websocket.send_bytes(frame)


def _live_view_query_pairs(query_params: Any) -> list[tuple[str, str]]:
    if hasattr(query_params, "multi_items"):
        items = query_params.multi_items()
    else:
        items = query_params.items()
    return [(key, value) for key, value in items if key != "token"]


def _live_view_upstream_ws_url(
    live_view_url: str,
    path: str,
    query_params: Any | None = None,
) -> str:
    upstream_base = live_view_url.rstrip("/")
    upstream_path = path.lstrip("/")
    upstream_url = (
        f"{upstream_base}/{upstream_path}" if upstream_path else f"{upstream_base}/"
    )
    if query_params is None:
        return upstream_url
    query = urlencode(_live_view_query_pairs(query_params))
    return f"{upstream_url}?{query}" if query else upstream_url


async def _should_forward_client_frame(
    *,
    session_id: str,
    tenant: TenantContext,
    control,
    frame: bytes,
) -> bool:
    if not is_input_frame(frame):
        return True
    if tenant.user_id is None:
        return False
    holder = await control.held_by(session_id)
    return holder == str(tenant.user_id)


async def _ensure_live_view_control(
    *,
    session_id: str,
    tenant: TenantContext,
    control,
) -> None:
    if tenant.user_id is None:
        raise HTTPException(
            status_code=403,
            detail="Browser live view requires browser control.",
        )
    holder = await control.held_by(session_id)
    if holder != str(tenant.user_id):
        raise HTTPException(
            status_code=403,
            detail="Browser live view requires browser control.",
        )


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


@router.get("/api/sessions/{session_id}/browser/preview.png")
@router.get("/sessions/{session_id}/browser/preview.png")
async def get_browser_preview(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> Response:
    resolver = request.app.state.browser_resolver
    resolved = await resolver.resolve(
        str(session_id),
        expected_org_id=str(tenant.org_id),
    )
    if resolved is None:
        raise HTTPException(status_code=404, detail="No browser for session")

    try:
        async with _browser_preview_client(resolved.endpoint.rest_url) as client:
            screenshot = await client.screenshot(viewport_only=True)
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


@router.api_route(
    "/api/sessions/{session_id}/browser/live/{path:path}",
    methods=["GET", "POST", "OPTIONS"],
)
@router.api_route(
    "/sessions/{session_id}/browser/live/{path:path}",
    methods=["GET", "POST", "OPTIONS"],
)
async def proxy_live_view(
    session_id: UUID,
    path: str,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> Response:
    resolver = request.app.state.browser_resolver
    control = request.app.state.browser_control
    resolved = await resolver.resolve(
        str(session_id),
        expected_org_id=str(tenant.org_id),
    )
    if resolved is None:
        raise HTTPException(status_code=404, detail="No browser for session")
    await _ensure_live_view_control(
        session_id=str(session_id),
        tenant=tenant,
        control=control,
    )

    upstream_base = (
        resolved.endpoint.live_view_url.replace("ws://", "http://", 1)
        .replace("wss://", "https://", 1)
        .rstrip("/")
    )
    upstream_url = f"{upstream_base}/{path}" if path else f"{upstream_base}/"
    forward_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower()
        not in {"host", "authorization", "cookie", "connection", "content-length"}
    }
    forward_params = _live_view_query_pairs(request.query_params)

    try:
        upstream = await _proxy_live_view_request(
            request.method,
            upstream_url,
            headers=forward_headers,
            params=forward_params,
            content=await request.body(),
        )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail="Browser live view is unreachable.",
        ) from exc

    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in {"connection", "transfer-encoding", "content-encoding"}
    }
    response = Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
    )
    token = request.query_params.get("token")
    if token:
        response.set_cookie(
            LIVE_VIEW_TOKEN_COOKIE,
            token,
            max_age=30 * 60,
            httponly=True,
            samesite="lax",
        )
    return response


@router.websocket("/api/sessions/{session_id}/browser/live/{path:path}")
@router.websocket("/sessions/{session_id}/browser/live/{path:path}")
async def proxy_live_view_ws(
    websocket: WebSocket,
    session_id: UUID,
    path: str,
) -> None:
    try:
        tenant = await authenticate_websocket_tenant(
            websocket.app,
            path=websocket.url.path,
            token=websocket.query_params.get("token"),
            cookies=websocket.cookies,
        )
    except HTTPException:
        await websocket.close(code=4401, reason="unauthenticated")
        return

    resolver = websocket.app.state.browser_resolver
    control = websocket.app.state.browser_control
    resolved = await resolver.resolve(
        str(session_id),
        expected_org_id=str(tenant.org_id),
    )
    if resolved is None:
        await websocket.close(code=4404, reason="no browser")
        return
    if tenant.user_id is None or await control.held_by(str(session_id)) != str(
        tenant.user_id
    ):
        await websocket.close(code=4403, reason="browser control required")
        return

    upstream_url = _live_view_upstream_ws_url(
        resolved.endpoint.live_view_url,
        path,
        websocket.query_params,
    )
    try:
        upstream = await _connect_live_view_ws(upstream_url)
    except Exception:
        await websocket.close(code=4502, reason="upstream unavailable")
        return

    requested_protocols = websocket.headers.get("sec-websocket-protocol", "")
    await websocket.accept(
        subprotocol="binary" if "binary" in requested_protocols else None,
    )

    async def client_to_upstream() -> None:
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                frame = _live_view_client_payload(message)
                if frame is None:
                    continue
                allowed = True
                if isinstance(frame, bytes):
                    allowed = await _should_forward_client_frame(
                        session_id=str(session_id),
                        tenant=tenant,
                        control=control,
                        frame=frame,
                    )
                if allowed:
                    await upstream.send(frame)
        except WebSocketDisconnect:
            return

    async def upstream_to_client() -> None:
        async for frame in upstream:
            await _send_live_view_frame_to_client(websocket, frame)

    tasks = [
        asyncio.create_task(client_to_upstream()),
        asyncio.create_task(upstream_to_client()),
    ]
    try:
        done, pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            task.result()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        with contextlib.suppress(Exception):
            await upstream.close()
        with contextlib.suppress(Exception):
            await websocket.close()
