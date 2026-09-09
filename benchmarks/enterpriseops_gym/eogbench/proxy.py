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
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from urllib.parse import urlsplit

# Hop-by-hop headers must not be relayed either direction.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length",
}


def context_headers(context: dict, auth_config: dict | None = None) -> dict[str, str]:
    """Match the pinned MCP client's context-to-header convention."""
    headers = {}
    auth = auth_config or {}
    if auth:
        if auth.get("type") not in ("bearer", "api_key") or not auth.get("token"):
            raise ValueError("Unsupported gym auth configuration")
        name = str(auth.get("header_name") or "Authorization").lower()
        headers[name] = ("Bearer " if auth["type"] == "bearer" else "") + str(auth["token"])
    for key, value in context.items():
        name = key.lower() if key.lower().startswith("x-") else "x-" + key.lower().replace("_", "-")
        headers[name] = str(value)
    if any(name in _HOP_BY_HOP or name == "x-database-id" or any(c in name + value for c in "\r\n")
           for name, value in headers.items()):
        raise ValueError("Gym context cannot override transport/database headers")
    return headers


def filtered_tools(payload: dict, allowed: frozenset[str]) -> dict:
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("tools"), list):
        return {**payload, "result": {**result, "tools": [tool for tool in result["tools"] if tool.get("name") in allowed]}}
    if "error" not in payload:
        raise ValueError("Invalid upstream tools/list response")
    return payload


class GymProxy:
    def __init__(self, upstream_base: str, port: int = 0) -> None:
        self._upstream = upstream_base.rstrip("/")
        self._database_id: str | None = None
        self._allowed: frozenset[str] | None = None
        self._context_headers: dict[str, str] = {}
        self._endpoint = "/mcp"
        self._scoped = False
        self._lock = threading.Lock()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:  # silence per-request noise
                pass

            def _json(self, payload, status=200, headers=None):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                for key, value in (headers or {}).items():
                    if key.lower() not in _HOP_BY_HOP | {"content-type", "content-encoding"}:
                        self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def _relay(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 8_000_000:
                    self._json({"error": "Request too large"}, 413)
                    return
                body = self.rfile.read(length) if length else None
                headers = {
                    k: v for k, v in self.headers.items()
                    if k.lower() not in _HOP_BY_HOP
                }
                with proxy._lock:
                    database_id, allowed = proxy._database_id, proxy._allowed
                    context = dict(proxy._context_headers)
                    endpoint, scoped = proxy._endpoint, proxy._scoped
                message = None
                if scoped:
                    # The agent-facing proxy never exposes gym database
                    # administration endpoints or an unscoped default DB.
                    if not database_id or urlsplit(self.path).path != endpoint or urlsplit(self.path).query:
                        self._json({"error": "No active task at this endpoint"}, 403)
                        return
                    if self.command == "POST":
                        try:
                            message = json.loads(body or b"")
                            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                                raise ValueError("Expected one JSON-RPC request")
                            method = message.get("method")
                            if method not in {"initialize", "notifications/initialized", "notifications/cancelled", "ping", "tools/list", "tools/call"}:
                                raise ValueError("Method unavailable in this task")
                            params = message.get("params") or {}
                            if method == "tools/call" and (not isinstance(params, dict) or params.get("name") not in allowed):
                                raise ValueError("Tool unavailable in this task")
                        except (ValueError, TypeError):
                            self._json({"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                                        "error": {"code": -32602, "message": "Request unavailable in this task"}})
                            return
                    headers = {k: v for k, v in headers.items() if not k.lower().startswith("x-") and k.lower() != "authorization"}
                    headers.update(context)
                if database_id:
                    headers = {k: v for k, v in headers.items() if k.lower() != "x-database-id"}
                    headers["x-database-id"] = database_id
                try:
                    with httpx.stream(
                        self.command,
                        f"{proxy._upstream}{self.path}",
                        headers=headers,
                        content=body,
                        timeout=httpx.Timeout(300.0, connect=10.0),
                    ) as resp:
                        if message and message.get("method") == "tools/list" and resp.is_success:
                            if "text/event-stream" in resp.headers.get("content-type", ""):
                                # Collapse the request's SSE result to JSON,
                                # which Streamable HTTP clients also accept.
                                data_lines = []
                                payload = None
                                for line in resp.iter_lines():
                                    if line.startswith("data:"):
                                        data_lines.append(line[5:].lstrip())
                                    elif not line and data_lines:
                                        event = json.loads("\n".join(data_lines))
                                        data_lines = []
                                        if event.get("id") == message.get("id"):
                                            payload = event
                                            break
                                if payload is None:
                                    raise ValueError("Missing tools/list result")
                            else:
                                resp.read()
                                payload = resp.json()
                            self._json(filtered_tools(payload, allowed), headers=resp.headers)
                            return
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

    def configure_task(self, database_id: str, selected_tools, restricted_tools=(), *, context=None, auth_config=None, endpoint="/mcp") -> None:
        allowed = frozenset(selected_tools) - frozenset(restricted_tools)
        if not allowed or not database_id or not endpoint.startswith("/") or urlsplit(endpoint).query:
            raise ValueError("Task needs a database, MCP endpoint, and allowed tools")
        headers = context_headers(context or {}, auth_config)
        with self._lock:
            self._scoped = True
            self._allowed, self._database_id = allowed, database_id
            self._context_headers, self._endpoint = headers, endpoint

    def start(self) -> "GymProxy":
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
