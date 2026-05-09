"""MCP tool-call circuit breaker tests."""

from __future__ import annotations

import json

from surogates.tools.mcp import client


def test_mcp_server_task_opens_circuit_after_failures() -> None:
    server = client.MCPServerTask("srv")
    server._circuit_failure_threshold = 2
    server._circuit_cooldown_seconds = 30

    server.record_call_failure()
    assert server.circuit_open_remaining() == 0

    server.record_call_failure()
    assert server.circuit_open_remaining() > 0

    server.record_call_success()
    assert server.circuit_open_remaining() == 0


def test_mcp_tool_handler_skips_call_when_circuit_open(monkeypatch) -> None:
    server = client.MCPServerTask("srv")
    server.session = object()
    server._circuit_failure_threshold = 1
    server._circuit_cooldown_seconds = 30
    server.record_call_failure()
    monkeypatch.setitem(client._servers, "srv", server)

    called = False

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("should not call server while circuit is open")

    monkeypatch.setattr(client, "_run_on_mcp_loop", fake_run)
    handler = client._make_tool_handler("srv", "tool", 1)

    result = json.loads(handler({}))

    assert called is False
    assert "circuit breaker is open" in result["error"]


def test_mcp_tool_handler_restarts_and_retries_oauth_401(monkeypatch) -> None:
    class AuthError(Exception):
        status_code = 401

    server = client.MCPServerTask("srv")
    server.session = object()
    server._auth_type = "oauth"
    monkeypatch.setitem(client._servers, "srv", server)

    calls: list[str] = []

    def fake_run(coro, **_kwargs):
        try:
            if not calls:
                calls.append("tool-failed")
                raise AuthError("Unauthorized")
            if len(calls) == 1:
                calls.append("restart")
                return None
            calls.append("tool-ok")
            return '{"result": "ok"}'
        finally:
            coro.close()

    monkeypatch.setattr(client, "_run_on_mcp_loop", fake_run)
    handler = client._make_tool_handler("srv", "tool", 1)

    result = json.loads(handler({}))

    assert result == {"result": "ok"}
    assert calls == ["tool-failed", "restart", "tool-ok"]
    assert server._circuit_failure_count == 0
