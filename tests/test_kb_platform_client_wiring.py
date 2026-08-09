"""Signature-level wiring check for the platform_client kwarg chain.

Mirrors test_media_gen_wiring.py: kb_search_pages needs platform_client
to reach _kb_search_pages_handler through dispatch, so every function
between AgentHarness and tools.dispatch must declare it by name or the
call silently arrives with platform_client=None and the search always
degrades. See tests/test_kb_search_tool.py for the functional proof
that data actually flows end to end.
"""

from __future__ import annotations

import inspect


def test_platform_client_threaded_through_harness_signatures():
    from surogates.harness import loop, streaming_executor, tool_exec

    for fn in (
        loop.AgentHarness.__init__,
        streaming_executor.StreamingToolExecutor.__init__,
        tool_exec.execute_tool_calls,
        tool_exec.execute_tool_calls_sequential,
        tool_exec.execute_tool_calls_concurrent,
        tool_exec.execute_single_tool,
    ):
        assert "platform_client" in inspect.signature(fn).parameters, fn.__qualname__
