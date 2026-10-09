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
from surogates.api.routes._commerce_turn import AllowanceReserveError, CommerceReserveError
from surogates.browser.control import HANDED_BACK_FROM, RESUMES, AcquireOutcome
from surogates.browser.shell import ShellSession
from surogates.devices.binding import device_of, is_binding_root
from surogates.harness.loop_pending import _hand_back_unread
from surogates.session.events import EventType
from surogates.session.store import BrowserControlBusy
from surogates.tenant.auth.oauth import OAuthTokens
from surogates.tenant.auth.middleware import (
    authenticate_websocket_tenant,
    get_current_tenant,
)
from surogates.tenant.context import TenantContext
from surogates.workstreams.spend import admit_turn

logger = logging.getLogger(__name__)

router = APIRouter()


class BrowserStateResponse(BaseModel):
    status: str
    control_owner: str | None
    live_view_path: str
    # On the user's computer, in a local-folder chat: no live view, and no lease to take.
    computer: bool = False


class BrowserControlRequest(BaseModel):
    action: str
    owner_user_id: str | None = None
    # With a release of a local-folder chat's browser: its user confirmed, in the desktop's own window
    # on the chat's computer, handing back the browser this chat holds.  The pane's word: a release
    # without it (the pane's own at a chat's opening, or one made for a chat that is gone) is told
    # for the pane alone.
    handed_back: bool = False


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


# What tells a local-folder chat's pane of its browser on the user's computer (surogates.devices.browser).
_COMPUTER_BROWSER = [EventType.BROWSER_PROVISIONED, EventType.BROWSER_DESTROYED, EventType.BROWSER_UNAVAILABLE]


def _its_own(tenant: TenantContext, org_id: UUID, user_id: UUID | None, session_id: UUID) -> bool:
    """Whether the caller is a local-folder chat's own user, or holds that session's own token.

    The chat's browser is on its user's own computer.  Nobody else of the organisation takes it
    over, hands it back or reads its state, an administrator and a service's token included: a
    session's routes answer on the organisation, and these two say what a person does at their
    own screen.  A token for one session, as a worker holds, answers for that session alone.
    """
    if org_id != tenant.org_id:
        return False
    if tenant.session_scope_id is not None:
        return tenant.session_scope_id == session_id
    return tenant.user_id is not None and tenant.user_id == user_id


async def _on_computer(app_state: Any, session_id: UUID, tenant: TenantContext) -> Any | None:
    """The session, when it is a local-folder chat's, whose browser is on the user's computer; None
    for one in the cloud.

    404, as for a chat that does not exist, for anyone but its own user and its own session's
    token.  The server keeps no browser, no live view and no lease for it: the desktop holds it.
    """
    store = getattr(app_state, "session_store", None)
    if store is None:
        return None
    try:
        session = await store.get_session(session_id)
    except Exception:
        return None
    if device_of(session.config) is None:
        return None
    if not _its_own(tenant, session.org_id, session.user_id, session_id):
        raise HTTPException(status_code=404, detail="No browser for session")
    return session


async def _chat_of(app_state: Any, session: Any, tenant: TenantContext) -> Any:
    """The chat a session on a computer belongs to: itself, or the chat it works under.

    The browser is taken over from a chat and handed back to it.  From a sub-agent's own view the
    take-over, the hand back and the turn a hand back gives are still its chat's: the sub-agent has
    no user to wait for, and its chat's agent is the one that goes on.
    """
    if is_binding_root(session.id, session.config):
        return session
    try:
        chat = await app_state.session_store.get_session(UUID(str(session.config["sandbox_root_session_id"])))
    except Exception:
        chat = None
    # One the caller may not ask of themselves, or that is not there, is answered as no chat is.
    if chat is None or not _its_own(tenant, chat.org_id, chat.user_id, chat.id):
        raise HTTPException(status_code=404, detail="No browser for session")
    return chat


async def _may_go_on(app_state: Any, chat: Any, tenant: TenantContext) -> bool:
    """Whether a hand back its user confirmed may give the chat's agent a turn.

    The confirmation is the desktop's own window, and it leaves the page nothing but a yes: the
    pane's word is all the server has of it.  It is taken only from the web client in the window of
    Surogate Desktop signed in on the chat's own computer, whose session is that sign-in's, bound to
    that computer: from anywhere else nothing was confirmed there.  That page's own code can still
    say so with nobody asked, as it can send a message.

    Then the chat must be able to take a turn: not stopped by its user or failed, with no turn
    under way, and within its user's limit, held as a typed message's turn is.  Where it cannot,
    the hand back is told for the pane alone: nothing is kept to be read with some later message.

    The chat is as it was read when the post arrived, and holding the turn asks ops: its user can
    stop it, or delete it, meanwhile.  Whether the turn is given is the store's to say, as it tells
    the hand back (``tell_browser_control``).
    """
    sign_in = tenant.oauth_family_id
    factory = getattr(app_state, "session_factory", None)
    if sign_in is None or factory is None:
        return False
    if await OAuthTokens(factory).computer(sign_in) != device_of(chat.config):
        return False
    if chat.status not in ("active", "completed") or await app_state.session_store.has_live_lease(chat.id):
        return False
    try:
        refused = await admit_turn(
            chat, "",
            platform_client=getattr(app_state, "platform_client", None),
            runtime_config_cache=getattr(app_state, "runtime_config_cache", None),
            session_store=app_state.session_store, session_factory=factory,
        )
    except (AllowanceReserveError, CommerceReserveError):
        logger.warning("Session %s: a hand back gives no turn while ops is unreachable", chat.id, exc_info=True)
        return False
    return refused is None


# What says whether the turn a hand back gave a chat is still to come.
_A_HAND_BACKS_TURN = [EventType.LLM_REQUEST, EventType.SESSION_RESUME, EventType.BROWSER_CONTROL_GRANTED]


async def _goes_on_already(app_state: Any, session_id: UUID) -> bool:
    """Whether a chat with no take-over standing has a hand back's turn still to come.

    What a repeat of a confirmed hand back is answered, as the one it repeats was: the chat open
    in two windows, or a post whose answer was lost.  Only while that turn is to come: once a
    request of the model's has read the hand back, or the chat was stopped, a release posted then
    hands nothing back and nobody goes on for it.
    """
    store = app_state.session_store
    if (await store.get_session(session_id)).status != "active":
        return False
    return _hand_back_unread(await store.get_events(session_id, types=_A_HAND_BACKS_TURN))


async def _computer_browser_state(app_state: Any, session_id: UUID) -> BrowserStateResponse:
    """A local-folder chat's browser, as its browser events say it.

    Open while a tab is: the session's own, or a sub-agent's, whose events the worker writes to its
    root chat's log too, each naming the session whose tab it is (surogates.devices.browser.tell_pane).
    A call that found no supported browser takes away its own session's tab, as its close does, and
    no other's: the chat says there is no browser only once no tab is left.

    The browser is on the user's computer, which the server does not watch: a tab the
    user closed there is still open here until the agent's next browser call says.
    """
    events = await app_state.session_store.get_events(session_id, types=_COMPUTER_BROWSER)
    tabs: set[str] = set()
    for event in events:
        of = str((event.data or {}).get("session_id") or session_id)
        if event.type == EventType.BROWSER_PROVISIONED.value:
            tabs.add(of)
        else:
            tabs.discard(of)
    if tabs:
        status = "live"
    elif events and events[-1].type == EventType.BROWSER_UNAVAILABLE.value:
        status = "unavailable"
    else:
        raise HTTPException(status_code=404, detail="No browser for session")
    return BrowserStateResponse(status=status, control_owner=None, live_view_path="", computer=True)


async def _tell(store: Any, chat_id: UUID, event_type: EventType, data: dict, **how: Any) -> bool | None:
    """Tell a chat of a take-over or a hand back, as its store does.

    One kept waiting behind another telling for the chat for longer than the store waits is
    answered 503, to be posted again: nothing was told, whatever the one ahead of it tells.  The
    browser is held, or the agent's again, on the computer all the same, and the pane says of a
    failed post that the chat could not be told.
    """
    try:
        return await store.tell_browser_control(chat_id, event_type, data, **how)
    except BrowserControlBusy:
        raise HTTPException(
            status_code=503, detail="The chat is being told of its browser by another request. Post it again.",
        )


# What tells a local-folder chat that its user took its browser over on the computer, and handed it back.
_COMPUTER_CONTROL = [EventType.BROWSER_CONTROL_GRANTED, EventType.BROWSER_CONTROL_RETURNED]


async def _told_taken_over(app_state: Any, session_id: UUID) -> bool:
    """Whether a local-folder chat was last told that its user took its browser over.

    There is no lease to ask: the chat's own events say, as they say its browser's state.
    """
    events = await app_state.session_store.get_events(session_id, types=_COMPUTER_CONTROL)
    return bool(events) and events[-1].type == EventType.BROWSER_CONTROL_GRANTED.value


async def _tell_the_agents_other_chats_handed_back(
    app_state: Any, session_id: UUID, tenant: TenantContext, emit: Any, released_by: str,
) -> None:
    """Tell the agent's other chats on the computer that their user no longer holds its browser.

    The agent's browser there is one for all its chats.  Its user takes it over from one, which is
    told, and hands it back from that one or, once that chat is gone from the computer, from
    another: the first would say they hold it for good.  So a hand back ends every take-over those
    chats were told of.  Each is told once, naming the chat it was made from, for its pane: its agent
    is not woken there.

    Only chats the caller may ask of themselves: a token for one session tells that one alone.  A
    chat that cannot be told leaves the hand back made.
    """
    try:
        session = await app_state.session_store.get_session(session_id)
        others = await app_state.session_store.chats_told_taken_over(
            device_id=device_of(session.config), org_id=session.org_id,
            agent_id=session.agent_id, user_id=session.user_id,
        )
    except Exception:
        logger.warning(
            "Could not look for the other chats to tell of the hand back made from session %s",
            session_id, exc_info=True,
        )
        return
    for other in others:
        if not _its_own(tenant, session.org_id, session.user_id, other):
            continue
        try:
            await emit(str(other), EventType.BROWSER_CONTROL_RETURNED, {
                "session_id": str(other), "released_by": released_by, "computer": True,
                HANDED_BACK_FROM: str(session_id),
            })
        except Exception:
            logger.warning(
                "Could not tell session %s of the hand back made from session %s",
                other, session_id, exc_info=True,
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

    await _require_session_agent(request.app.state, session_id, tenant)
    if await _on_computer(request.app.state, session_id, tenant) is not None:
        return await _computer_browser_state(request.app.state, session_id)
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
) -> dict[str, str | bool]:
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
    # A local-folder chat's browser is on the user's computer, which holds its pause.
    computer = await _on_computer(request.app.state, session_id, tenant)
    if computer is None and await resolver.resolve(str(session_id), expected_org_id=str(tenant.org_id)) is None:
        raise HTTPException(status_code=404, detail="No browser for session")

    owner_user_id = body.owner_user_id if _route_prefix(request) == "/v1/api" else None
    if owner_user_id is None and tenant.user_id is not None:
        owner_user_id = str(tenant.user_id)
    if owner_user_id is None:
        raise HTTPException(
            status_code=403,
            detail="Browser control requires a user identity.",
        )

    if computer is not None:
        # Told to the chat as the cloud's are, its user having taken it over or handed it back in the
        # desktop: there is no lease here. Each is told once: a take-over while one stands, and a
        # hand back with none standing, answer as done and tell the chat nothing. Two posts at once
        # tell it one: the store takes them in turn, on one connection each.
        store = request.app.state.session_store
        chat = await _chat_of(request.app.state, computer, tenant)
        sid = str(chat.id)
        if body.action == "acquire":
            taken_over = {"session_id": sid, "owner_user_id": owner_user_id, "computer": True}
            if await _tell(store, chat.id, EventType.BROWSER_CONTROL_GRANTED, taken_over) is None:
                return {"outcome": "refreshed", "owner_user_id": owner_user_id}
            return {"outcome": "granted", "owner_user_id": owner_user_id}
        # A release answers whether the agent goes on by itself: only at a hand back its user
        # confirmed, of a take-over that stands, to a chat that can take a turn as it is told.
        if not await _told_taken_over(request.app.state, chat.id):
            return {"outcome": "released", RESUMES: body.handed_back and await _goes_on_already(request.app.state, chat.id)}
        confirmed = body.handed_back and await _may_go_on(request.app.state, chat, tenant)
        told = {"session_id": sid, "released_by": owner_user_id, "computer": True}
        # The turn is given with the telling, or not at all: the chat is made active as a typed
        # message makes one whose turn had ended, and the resume written is the turn, and what the
        # agent reads the hand back from.
        goes_on = await _tell(store, chat.id, EventType.BROWSER_CONTROL_RETURNED, told, gives_a_turn=confirmed)
        if goes_on is None:
            # Another post handed it back meanwhile: that one told the chat, and gave what it gave.
            return {"outcome": "released", RESUMES: body.handed_back and await _goes_on_already(request.app.state, chat.id)}
        # The agent's other chats there that still said their user held the browser are told too.
        await _tell_the_agents_other_chats_handed_back(request.app.state, chat.id, tenant, emit, owner_user_id)
        if goes_on:
            # All that can still be lost is this, and the sweeper finds a chat left so.
            await wake(sid)
        return {"outcome": "released", RESUMES: goes_on}

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
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        # Nothing ANSWERED at the address the registry gave -- connection
        # refused or nothing listening -- so the entry is wrong: drop it and
        # report the browser as absent. Deliberately narrower than
        # TransportError: ReadTimeout is a subclass, and a screenshot of a
        # page mid-load times out on read while the browser is entirely
        # alive. Classifying that as "unreachable" deleted live browsers'
        # entries, which is how a working session came to say
        # "Browser disconnected".
        await resolver.forget_unreachable(str(session_id))
        raise HTTPException(
            status_code=404,
            detail="No browser for session",
        ) from exc
    except Exception as exc:
        # Reached the browser and it failed anyway -- a page mid-navigation, a
        # slow render, a screenshot timeout. That is not evidence the browser
        # is gone, and pruning here would delete a healthy browser's entry and
        # take the pane down with it.
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
    except Exception as exc:
        logger.warning(
            "browser shell rejected: cannot reach CDP at %s",
            resolved.endpoint.cdp_url,
            exc_info=True,
        )
        # Prune only when nothing was LISTENING after the full readiness
        # window -- a refused connection is a stale entry, but a reachable
        # browser that answered strangely (bad /json/version, an envoy hiccup)
        # is still a browser, and deleting its entry would kill a live one.
        if isinstance(
            exc, (httpx.ConnectError, httpx.ConnectTimeout, ConnectionError, OSError)
        ):
            await resolver.forget_unreachable(str(session_id))
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
