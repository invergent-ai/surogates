"""Malformed tool arguments are handled safely during execution and streaming."""

from __future__ import annotations

import json
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.harness.tool_exec import (
    execute_single_tool,
)
from surogates.harness.llm_call import call_llm_streaming_inner
from surogates.tools.registry import ToolRegistry, ToolSchema


@pytest.mark.asyncio
async def test_execute_single_tool_rejects_unrepairable_json_without_dispatch() -> None:
    registry = ToolRegistry()
    handler = AsyncMock(return_value='{"ok": true}')
    registry.register(
        "write_file",
        ToolSchema(
            name="write_file",
            description="write file",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            },
        ),
        handler=handler,
    )
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=[1, 2])
    store.advance_harness_cursor = AsyncMock()
    session = SimpleNamespace(
        id=uuid4(),
        config={"workspace_path": ""},
        agent_id="test-agent",
    )
    lease = SimpleNamespace(lease_token=uuid4())
    tenant = MagicMock(asset_root="/tmp/test")

    result = await execute_single_tool(
        {
            "id": "tc_bad_json",
            "function": {
                "name": "write_file",
                "arguments": '{"path": "a.txt", "content": "unterminated}',
            },
        },
        session=session,
        lease=lease,
        store=store,
        tools=registry,
        tenant=tenant,
    )

    handler.assert_not_called()
    parsed = json.loads(result["content"])
    assert parsed["error"].startswith("Invalid JSON arguments")
    assert parsed["tool"] == "write_file"
    assert "unterminated" in parsed["detail"].lower()


@pytest.mark.asyncio
async def test_streaming_marks_incomplete_tool_call_arguments_as_partial() -> None:
    class _Stream:
        def __init__(self, chunks):
            self._chunks = chunks
            self._idx = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._idx >= len(self._chunks):
                raise StopAsyncIteration
            chunk = self._chunks[self._idx]
            self._idx += 1
            return chunk

    def _chunk(tool_delta=None, finish_reason=None):
        delta = SimpleNamespace(content=None, role=None, tool_calls=tool_delta)
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
            model="gpt-4o",
            usage=None,
        )

    tool_delta = SimpleNamespace(
        index=0,
        id="call_1",
        function=SimpleNamespace(name="write_file", arguments='{"path": "a.txt"'),
    )
    stream = _Stream([_chunk([tool_delta]), _chunk(finish_reason="tool_calls")])
    llm_client = MagicMock()
    llm_client.chat.completions.create = AsyncMock(return_value=stream)
    store = AsyncMock()

    _msg, usage = await asyncio.wait_for(
        call_llm_streaming_inner(
            session=SimpleNamespace(id=uuid4(), config={}, model="gpt-4o"),
            create_kwargs={"model": "gpt-4o", "messages": []},
            iteration=1,
            llm_client=llm_client,
            store=store,
            interrupt_check=lambda: False,
        ),
        timeout=3,
    )

    assert usage["finish_reason"] == "tool_calls"
    assert usage["partial_tool_call"] is True
    assert usage["partial_tool_names"] == ["write_file"]
