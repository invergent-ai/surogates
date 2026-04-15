"""ASGI middleware that strips a leading ``/api`` path prefix.

The web SPA posts to ``/api/...`` in both dev (vite proxy) and prod
(same-origin).  In dev, the vite proxy rewrites the prefix away before
the request reaches the backend; in prod there is no proxy, so the
middleware performs the same rewrite inside FastAPI.

Routing, auth, and every downstream middleware therefore see the
canonical ``/v1/...`` path regardless of how the client addressed it.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

_PREFIX = "/api"


class StripApiPrefixMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send,
    ) -> None:
        if scope["type"] == "http":
            path: str = scope["path"]
            if path == _PREFIX or path.startswith(_PREFIX + "/"):
                new_path = path[len(_PREFIX):] or "/"
                scope = {
                    **scope,
                    "path": new_path,
                    "raw_path": new_path.encode("utf-8"),
                }
        await self.app(scope, receive, send)
