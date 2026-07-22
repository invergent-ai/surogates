"""Same-origin Firebase auth helpers for the agent web app.

``signInWithPopup`` completes through helper pages served from the
Firebase ``authDomain`` (``/__/auth/*``). When that domain differs
from the page origin, browsers that partition third-party storage
(Firefox ETP, Safari, Chrome's cookie phase-out) sever the popup's
completion channel and sign-in hangs forever.

In production every agent web app is served at
``<slug>.<base-domain>`` by this API (wildcard ingress), so this
route makes the app's own origin serve the helpers: it resolves the
agent from the request (Host subdomain), looks up the project's
Firebase config, and reverse-proxies to the project's real
``firebaseapp.com`` domain. The SPA then uses ``location.host`` as
``authDomain``, keeping the whole flow first-party. The agent's
domain must be added to the Firebase project's authorized domains.

Dev keeps the vite ``/__`` proxy (no Host slug to resolve there);
this route is the production equivalent. The upstream host always
comes from the stored project config — never from the request — so
this is a pinned proxy, not an open one.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from surogates.runtime import AgentRuntimeContext, agent_runtime_context_dep

logger = logging.getLogger(__name__)

router = APIRouter()

# Only the Firebase helper namespaces are proxied.
_ALLOWED_PREFIXES = ("auth/", "firebase/")

# Hop-by-hop and origin-bound headers that must not be forwarded
# either direction.
_SKIP_REQUEST_HEADERS = {
    "host",
    "connection",
    "content-length",
    "accept-encoding",
    "cookie",
    "transfer-encoding",
}
_SKIP_RESPONSE_HEADERS = {
    "connection",
    "content-length",
    "content-encoding",
    "transfer-encoding",
    "set-cookie",
}

_PROXY_TIMEOUT_SECONDS = 15.0


def _proxy_client(request: Request) -> httpx.AsyncClient:
    client = getattr(request.app.state, "firebase_helper_client", None)
    if client is None:
        client = httpx.AsyncClient(
            timeout=_PROXY_TIMEOUT_SECONDS, follow_redirects=False,
        )
        request.app.state.firebase_helper_client = client
    return client


@router.api_route("/__/{helper_path:path}", methods=["GET", "POST"])
async def firebase_auth_helpers(
    helper_path: str,
    request: Request,
    agent_runtime: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> Response:
    if not helper_path.startswith(_ALLOWED_PREFIXES):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Not found",
        )

    cache = getattr(request.app.state, "firebase_config_cache", None)
    project_id = getattr(agent_runtime, "project_id", None)
    fb = None
    if cache is not None and project_id:
        try:
            fb = await cache.get(project_id)
        except LookupError:
            fb = None
    if fb is None or not getattr(fb, "auth_domain", None):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Firebase auth is not configured.",
        )

    upstream = httpx.URL(
        f"https://{fb.auth_domain}/__/{helper_path}",
        query=request.url.query.encode() or None,
    )
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _SKIP_REQUEST_HEADERS
    }
    body = await request.body()
    client = _proxy_client(request)
    try:
        proxied = await client.request(
            request.method, upstream, headers=headers, content=body or None,
        )
    except httpx.HTTPError:
        logger.warning(
            "Firebase helper proxy failed for %s", upstream, exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Firebase auth helpers are unreachable.",
        )
    response_headers = {
        k: v
        for k, v in proxied.headers.items()
        if k.lower() not in _SKIP_RESPONSE_HEADERS
    }
    return Response(
        content=proxied.content,
        status_code=proxied.status_code,
        headers=response_headers,
    )
