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
