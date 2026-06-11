"""Signature-level wiring checks for the media_gen kwarg chain.

The pass-through edits are mechanical (one line next to each
``vision_llm_client``); these tests catch a missed signature, and the
grep check in the plan proves the kwargs dict actually carries
media_gen.
"""

from __future__ import annotations

import inspect


def test_media_gen_threaded_through_harness_signatures():
    from surogates.harness import loop, streaming_executor, tool_exec

    for fn in (
        loop.AgentHarness.__init__,
        streaming_executor.StreamingToolExecutor.__init__,
        tool_exec.execute_tool_calls,
        tool_exec.execute_tool_calls_sequential,
        tool_exec.execute_tool_calls_concurrent,
        tool_exec.execute_single_tool,
    ):
        assert "media_gen" in inspect.signature(fn).parameters, fn.__qualname__
