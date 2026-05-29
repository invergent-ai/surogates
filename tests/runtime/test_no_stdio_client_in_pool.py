"""Plan 5 / Task 13 source-level regression.

``stdio_client(...)`` is the mcp SDK primitive that wraps a long-
lived subprocess.  Plan 5 / Task 11 routes every MCP call through
:meth:`MCPCallSandbox.mcp_session`, which uses ``stdio_client`` for
ONE call inside an async context manager — the subprocess dies on
context exit, not on idle eviction.

The remaining ``stdio_client`` call sites are:

* :mod:`surogates.mcp_proxy.sandbox` — the per-call sandbox path
  (intentional; Plan 5's whole point).
* :mod:`surogates.tools.mcp.client` — the legacy module-level
  client used by the in-process worker dev mode and by
  ``ConnectionPool.ensure_connected`` for tool discovery.  This
  module keeps long-lived ``stdio_client`` subprocesses alive
  in the ``_servers`` dict; Plan 6 retires it.

This regression ensures the per-call hot path (``pool.py``) does
NOT acquire ``stdio_client`` directly — the long-lived reuse via
``server.session`` is gone from the route, and a future refactor
that re-introduces ``stdio_client(`` into ``pool.py`` would
indicate the pool is again spawning its own subprocesses outside
the sandbox.
"""

from __future__ import annotations

import re
from pathlib import Path


def test_pool_does_not_invoke_stdio_client():
    """``surogates/mcp_proxy/pool.py`` must not call
    ``stdio_client(...)`` — the per-call path goes through
    :class:`MCPCallSandbox` in :mod:`surogates.mcp_proxy.sandbox`."""
    pattern = re.compile(r"\bstdio_client\(")
    path = Path("surogates/mcp_proxy/pool.py")
    text = path.read_text(encoding="utf-8")
    offenders = []
    for m in pattern.finditer(text):
        line = text[: m.start()].count("\n") + 1
        offenders.append(f"{path}:{line}")
    assert not offenders, (
        "Plan 5 / Task 13 — pool.py must not invoke stdio_client; "
        "use MCPCallSandbox in routes.py for per-call subprocess "
        "isolation.  Offending lines:\n" + "\n".join(offenders)
    )


def test_route_handler_does_not_invoke_pool_call_tool():
    """The ``call_tool`` route handler no longer reaches into the
    long-lived pool.call_tool helper for the stdio path.  Plan
    5 / Task 11 routed the handler through
    :meth:`MCPCallSandbox.mcp_session`; this regression catches a
    future revert in the handler itself.

    The module-level ``_execute_call`` helper still uses
    ``pool.call_tool`` as an explicit fallback for HTTP / SSE
    transports (no subprocess to isolate, so reusing the long-
    lived session is safe).  The check scopes to the route handler
    function body, not the module, so the HTTP fallback is allowed."""
    import inspect

    import surogates.mcp_proxy.routes as routes

    handler_src = inspect.getsource(routes.call_tool)
    assert "pool.call_tool(" not in handler_src, (
        "Plan 5 / Task 13 — routes.call_tool must not invoke "
        "pool.call_tool directly; the per-call sandbox replaced it."
    )


def test_route_uses_mcp_session():
    """Positive regression: the route does use
    :meth:`MCPCallSandbox.mcp_session` for the per-call path."""
    path = Path("surogates/mcp_proxy/routes.py")
    text = path.read_text(encoding="utf-8")
    assert "MCPCallSandbox" in text
    assert "mcp_session" in text
