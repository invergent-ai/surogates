"""Plan 5 / Task 11 source-level regression.

The call_tool route spawns a fresh MCPCallSandbox per call instead
of reusing the long-lived MCPServerTask.session.  Source-level
inspection is sufficient — the actual subprocess-per-call behavior
is end-to-end-tested in the integration tier (where a real MCP
server can be spawned).
"""

from __future__ import annotations

import inspect


def test_call_tool_constructs_mcp_call_sandbox():
    """The route source must reference MCPCallSandbox so a future
    refactor can't quietly revert to the long-lived reuse path."""
    import surogates.mcp_proxy.routes as routes

    src = inspect.getsource(routes.call_tool)
    assert "MCPCallSandbox" in src
    # The legacy long-lived reuse marker is gone — call_tool no
    # longer reaches into pool.call_tool() to use a cached session.
    assert "MCPServerTask.session.call_tool" not in src
    # The pool's call_tool helper (which used the long-lived session)
    # is no longer invoked from the route.
    assert "pool.call_tool(" not in src


def test_call_tool_emits_policy_mcp_call():
    """Plan 5 / Task 12.  The call_tool route emits POLICY_MCP_CALL
    with the per-request agent_id + the call outcome (success /
    timeout / rlimit / error) so compliance can answer 'which agent
    invoked tool X on server Y, when?' from the audit log alone."""
    import inspect
    import surogates.mcp_proxy.routes as routes

    src = inspect.getsource(routes.call_tool)
    assert "POLICY_MCP_CALL" in src
    assert "audit_store" in src
    assert "ctx.agent_id" in src


# ---------------------------------------------------------------------------
# Plan 5 review follow-ups: outcome classification + HTTP fallback
# ---------------------------------------------------------------------------


def _make_server_config(transport: str = "stdio") -> dict:
    if transport == "stdio":
        return {
            "transport": "stdio", "command": "cat", "args": [], "env": {},
        }
    return {
        "transport": transport, "url": "https://example.invalid",
        "env": {},
    }


class _RecordingPool:
    """Captures pool.call_tool invocations so the HTTP-fallback path
    can be asserted without standing up a real long-lived session."""

    def __init__(self, result: str) -> None:
        self.calls: list[dict] = []
        self._result = result

    async def call_tool(self, **kwargs):
        self.calls.append(kwargs)
        return self._result


import pytest  # noqa: E402


@pytest.mark.asyncio
async def test_execute_call_http_transport_falls_back_to_pool():
    """Review fix: HTTP / SSE MCP servers have no subprocess to
    isolate, so the call_tool path falls back to the legacy long-
    lived session via pool.call_tool.  Without this, every HTTP
    MCP server returned a structured error after Plan 5 -- which
    is a regression for any existing tenant configuration."""
    from surogates.mcp_proxy import routes

    pool = _RecordingPool(result='{"ok": true}')

    result_text, outcome = await routes._execute_call(
        pool=pool,
        org_id="o-1",
        user_id="u-1",
        server_config=_make_server_config("http"),
        tool_name="ping",
        arguments={"x": 1},
        meta={"chat_user_id": "c-1"},
    )

    assert result_text == '{"ok": true}'
    assert outcome == routes._OUTCOME_SUCCESS
    assert pool.calls == [{
        "org_id": "o-1", "user_id": "u-1", "tool_name": "ping",
        "arguments": {"x": 1}, "meta": {"chat_user_id": "c-1"},
    }]


@pytest.mark.asyncio
async def test_execute_call_http_transport_propagates_tool_error():
    """The HTTP fallback path must classify a legacy error envelope
    as outcome=tool_error so the audit dashboard does not see it as
    a transport failure (alerting noise)."""
    from surogates.mcp_proxy import routes

    pool = _RecordingPool(result='{"error": "permission denied"}')

    _result_text, outcome = await routes._execute_call(
        pool=pool,
        org_id="o-1",
        user_id="u-1",
        server_config=_make_server_config("http"),
        tool_name="ping",
        arguments={},
        meta=None,
    )

    assert outcome == routes._OUTCOME_TOOL_ERROR


@pytest.mark.asyncio
async def test_execute_call_stdio_missing_command_returns_transport_error():
    """Defensive: a config row that somehow has transport=stdio but
    no command (e.g. a botched manual DB edit) must surface as a
    transport_error so dashboards alert rather than masquerading as
    a tool-level failure."""
    from surogates.mcp_proxy import routes

    pool = _RecordingPool(result="unused")

    result_text, outcome = await routes._execute_call(
        pool=pool,
        org_id="o-1",
        user_id="u-1",
        server_config={"transport": "stdio"},  # no command
        tool_name="ping",
        arguments={},
        meta=None,
    )

    assert outcome == routes._OUTCOME_TRANSPORT_ERROR
    assert "missing command" in result_text


@pytest.mark.asyncio
async def test_execute_call_stdio_spawn_failure_returns_transport_error():
    """When the MCP SDK's stdio_client fails to spawn the subprocess
    (e.g. command not found), the helper must return a transport-
    error envelope -- distinct from tool_error -- so the route's
    audit emit gets the right outcome label."""
    from surogates.mcp_proxy import routes

    pool = _RecordingPool(result="unused")

    result_text, outcome = await routes._execute_call(
        pool=pool,
        org_id="o-1",
        user_id="u-1",
        server_config={
            "transport": "stdio",
            "command": "/nonexistent/binary",
            "args": [],
            "env": {},
        },
        tool_name="ping",
        arguments={},
        meta=None,
    )

    assert outcome == routes._OUTCOME_TRANSPORT_ERROR
    assert "error" in result_text


def test_outcome_constants_exist():
    """Named outcome constants are part of the audit contract --
    dashboards filter on these spellings."""
    from surogates.mcp_proxy import routes

    assert routes._OUTCOME_SUCCESS == "success"
    assert routes._OUTCOME_TOOL_ERROR == "tool_error"
    assert routes._OUTCOME_TRANSPORT_ERROR == "transport_error"
