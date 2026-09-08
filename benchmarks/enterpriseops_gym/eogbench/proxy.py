"""Header-injecting forwarder between the tunnel and the gym server.

The gym scopes all state by an ``x-database-id`` header; the platform's
mcp-proxy dereferences a registered URL as-is and cannot add custom
headers. This forwarder sits on a local port, injects the current
task's database id into every request, and streams the response back --
SSE included -- so the tunnel can front one stable port for the whole
run while the database id changes per task.

Plain stdlib threading server: MCP streamable-HTTP is request/response
plus SSE, both of which survive chunk-relay. ``set_database_id`` swaps
the injected id between tasks without restarting the port.
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

# Hop-by-hop headers must not be relayed either direction.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length",
}


class GymProxy:
    def __init__(self, upstream_base: str, port: int = 0) -> None:
        self._upstream = upstream_base.rstrip("/")
        self._database_id: str | None = None
        self._lock = threading.Lock()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:  # silence per-request noise
                pass

            def _relay(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                headers = {
                    k: v for k, v in self.headers.items()
                    if k.lower() not in _HOP_BY_HOP
                }
                with proxy._lock:
                    if proxy._database_id:
                        headers["x-database-id"] = proxy._database_id
                try:
                    with httpx.stream(
                        self.command,
                        f"{proxy._upstream}{self.path}",
                        headers=headers,
                        content=body,
                        timeout=httpx.Timeout(300.0, connect=10.0),
                    ) as resp:
                        self.send_response(resp.status_code)
                        for k, v in resp.headers.items():
                            if k.lower() not in _HOP_BY_HOP:
                                self.send_header(k, v)
                        self.send_header("Connection", "close")
                        self.end_headers()
                        for chunk in resp.iter_raw():
                            if chunk:
                                self.wfile.write(chunk)
                                self.wfile.flush()
                except Exception as exc:  # noqa: BLE001 - surface to caller
                    try:
                        self.send_response(502)
                        self.send_header("Content-Type", "text/plain")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(
                            f"gym proxy relay error: {exc}".encode()
                        )
                    except Exception:  # noqa: BLE001 - client already gone
                        pass

            do_GET = _relay
            do_POST = _relay
            do_DELETE = _relay

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def set_database_id(self, database_id: str | None) -> None:
        with self._lock:
            self._database_id = database_id

    def start(self) -> "GymProxy":
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
