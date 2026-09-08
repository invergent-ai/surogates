"""The header-injecting forwarder, end to end against a local upstream."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from eogbench.proxy import GymProxy


@pytest.fixture
def upstream():
    """A local server that echoes method, path, and selected headers."""
    class Echo(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _echo(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            payload = json.dumps({
                "method": self.command,
                "path": self.path,
                "database_id": self.headers.get("x-database-id"),
                "auth": self.headers.get("Authorization"),
                "body": body.decode() or None,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = _echo
        do_POST = _echo

    server = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_injects_database_id_and_relays(upstream):
    proxy = GymProxy(upstream).start()
    try:
        proxy.set_database_id("db_task_42")
        resp = httpx.post(
            f"{proxy.local_url}/mcp",
            content=b'{"jsonrpc": "2.0"}',
            headers={"Authorization": "Bearer tok"},
            timeout=10,
        )
        echoed = resp.json()
        assert echoed["method"] == "POST"
        assert echoed["path"] == "/mcp"
        assert echoed["database_id"] == "db_task_42"
        assert echoed["auth"] == "Bearer tok"  # client headers preserved
        assert echoed["body"] == '{"jsonrpc": "2.0"}'
    finally:
        proxy.stop()


def test_database_id_swaps_between_tasks(upstream):
    proxy = GymProxy(upstream).start()
    try:
        proxy.set_database_id("db_a")
        assert httpx.get(f"{proxy.local_url}/x", timeout=10).json()["database_id"] == "db_a"
        proxy.set_database_id("db_b")
        assert httpx.get(f"{proxy.local_url}/x", timeout=10).json()["database_id"] == "db_b"
        proxy.set_database_id(None)
        assert httpx.get(f"{proxy.local_url}/x", timeout=10).json()["database_id"] is None
    finally:
        proxy.stop()


def test_unreachable_upstream_yields_502():
    proxy = GymProxy("http://127.0.0.1:1").start()
    try:
        resp = httpx.get(f"{proxy.local_url}/mcp", timeout=10)
        assert resp.status_code == 502
        assert "relay error" in resp.text
    finally:
        proxy.stop()
