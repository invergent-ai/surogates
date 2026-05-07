"""Per-channel CORS handling for ``/v1/website/*``.

The global :class:`fastapi.middleware.cors.CORSMiddleware` applies a
single static allow-list to every path.  The website channel needs a
separate, configurable allow-list that may differ from the rest of
the API surface (the embedding domains rarely overlap with API
consumers), so this middleware takes over CORS for website paths and
the global middleware only governs the rest of the API.

Preflight (``OPTIONS``) requests cannot be authenticated -- the
browser strips cookies and the custom Authorization header from
preflights by spec -- so the middleware answers them permissively.
Preflight permissiveness is not a security concession because the
actual authorization (publishable key + origin match, or cookie +
origin match) happens on the follow-up request.  The route handlers
reject mismatched origins with a 403 that the browser surfaces to JS.

On the response side, the middleware echoes the request's ``Origin``
back in ``Access-Control-Allow-Origin`` and sets
``Access-Control-Allow-Credentials: true`` so the browser accepts the
``Set-Cookie`` from bootstrap and includes it on subsequent requests.
``Vary: Origin`` prevents intermediary caches from serving one
origin's response to another origin.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

if TYPE_CHECKING:
    from fastapi import FastAPI, Request

logger = logging.getLogger(__name__)


_WEBSITE_PATH_PREFIX = "/v1/website/"
# Headers the browser embed will send: Authorization (bootstrap),
# X-CSRF-Token (messages), Content-Type (JSON body), plus Accept for SSE.
_ALLOWED_HEADERS = "Authorization, Content-Type, X-CSRF-Token, Accept"
_ALLOWED_METHODS = "GET, POST, OPTIONS"
# Cache preflight for 10 minutes.  Any shorter and an active chat
# triggers a fresh OPTIONS on every message; any longer and a change
# to the allowed-header set takes too long to converge.
_PREFLIGHT_MAX_AGE = "600"


def _is_website_path(path: str) -> bool:
    return path.startswith(_WEBSITE_PATH_PREFIX)


def setup_website_cors_middleware(app: FastAPI) -> None:
    """Attach the per-path CORS middleware for the website channel."""

    @app.middleware("http")
    async def _website_cors_middleware(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if not _is_website_path(request.url.path):
            return await call_next(request)

        origin = request.headers.get("origin")

        if request.method == "OPTIONS":
            # Preflight: answer directly, short-circuit the rest of
            # the stack.  We echo whatever Origin the browser sent;
            # authorization happens on the real request that follows.
            headers = {
                "Access-Control-Allow-Methods": _ALLOWED_METHODS,
                "Access-Control-Allow-Headers": _ALLOWED_HEADERS,
                "Access-Control-Allow-Credentials": "true",
                "Access-Control-Max-Age": _PREFLIGHT_MAX_AGE,
                "Vary": "Origin",
            }
            if origin:
                headers["Access-Control-Allow-Origin"] = origin
            return Response(status_code=204, headers=headers)

        response = await call_next(request)
        if origin:
            # Overwrite -- Starlette's CORSMiddleware may have also set
            # a header (we keep it in the stack for non-website paths)
            # but website paths need ``Access-Control-Allow-Credentials:
            # true`` and an explicit echoed origin (not ``*``) so the
            # browser accepts the cookie.  Authority for *whether* this
            # origin is permitted is enforced inside the route handlers
            # against ``settings.website.allowed_origins`` -- this
            # middleware only rewrites the response headers so the
            # browser doesn't drop the response on CORS grounds before
            # the handler's 401/403 reaches JS.
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            existing_vary = response.headers.get("Vary", "")
            if "Origin" not in existing_vary:
                response.headers["Vary"] = (
                    f"{existing_vary}, Origin" if existing_vary else "Origin"
                )
        return response
