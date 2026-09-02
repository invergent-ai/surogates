"""The ``/api`` prefix strip must cover websockets, not just HTTP.

The web SPA addresses the backend as ``/api/v1/...`` for everything it
opens — fetches and websockets alike. The middleware rewrote only HTTP
scopes, so the browser-shell websocket arrived as ``/api/v1/...``, matched
no route, and the pane reported "Browser disconnected" while the preview
image (an ordinary GET) loaded fine on the very same session.
"""

from __future__ import annotations

from surogates.api.middleware.api_prefix import StripApiPrefixMiddleware


class Recorder:
    """Captures the path the wrapped app is finally called with."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.raw_paths: list[bytes] = []

    async def __call__(self, scope, receive, send) -> None:
        if "path" in scope:
            self.paths.append(scope["path"])
            self.raw_paths.append(scope.get("raw_path", b""))


async def _call(scope_type: str, path: str) -> Recorder:
    recorder = Recorder()
    middleware = StripApiPrefixMiddleware(recorder)
    await middleware({"type": scope_type, "path": path}, None, None)
    return recorder


class TestHttp:
    async def test_prefix_is_stripped(self) -> None:
        recorder = await _call("http", "/api/v1/sessions/s-1")
        assert recorder.paths == ["/v1/sessions/s-1"]

    async def test_unprefixed_path_is_untouched(self) -> None:
        recorder = await _call("http", "/v1/sessions/s-1")
        assert recorder.paths == ["/v1/sessions/s-1"]


class TestWebsocket:
    async def test_prefix_is_stripped(self) -> None:
        # The browser shell: /api/v1/sessions/{id}/browser/shell must reach
        # the route mounted at /v1, exactly as the HTTP calls beside it do.
        recorder = await _call(
            "websocket", "/api/v1/sessions/s-1/browser/shell"
        )
        assert recorder.paths == ["/v1/sessions/s-1/browser/shell"]
        assert recorder.raw_paths == [b"/v1/sessions/s-1/browser/shell"]

    async def test_unprefixed_path_is_untouched(self) -> None:
        recorder = await _call("websocket", "/v1/sessions/s-1/browser/shell")
        assert recorder.paths == ["/v1/sessions/s-1/browser/shell"]


class TestEdges:
    async def test_bare_prefix_becomes_root(self) -> None:
        for scope_type in ("http", "websocket"):
            recorder = await _call(scope_type, "/api")
            assert recorder.paths == ["/"]

    async def test_a_path_merely_starting_with_api_is_untouched(self) -> None:
        # ``/apiary`` is not the ``/api`` prefix; only a whole segment is.
        for scope_type in ("http", "websocket"):
            recorder = await _call(scope_type, "/apiary/v1")
            assert recorder.paths == ["/apiary/v1"]

    async def test_other_scopes_pass_through(self) -> None:
        # Lifespan carries no path; the middleware must not invent one.
        recorder = Recorder()
        middleware = StripApiPrefixMiddleware(recorder)
        await middleware({"type": "lifespan"}, None, None)
        assert recorder.paths == []
