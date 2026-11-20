"""An MCP tool that answers in structured content only reaches the model with that content.

The proxy built a tool's answer from its text blocks alone, so a tool answering only in
``structuredContent`` (MCP 2025-06-18; e.g. FastMCP tools returning a dict or a model) gave the
model an empty result.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

from surogates.mcp_proxy.pool import ConnectionPool
from surogates.tools.mcp import client as mcp_client

pytestmark = pytest.mark.asyncio


def _server(result: CallToolResult):
    async def call_tool(name, arguments=None, meta=None):
        return result
    return SimpleNamespace(session=SimpleNamespace(call_tool=call_tool))


@pytest.fixture(autouse=True)
def _no_mcp_loop(monkeypatch):
    monkeypatch.setattr(mcp_client, "_mcp_loop", None, raising=False)


async def test_a_structured_only_answer_is_the_result():
    rows = {"intakes": [{"id": "IN-7", "depot": "North", "litres": 412.5}], "total": 1}
    out = await ConnectionPool._execute_tool_call(_server(CallToolResult(content=[], structuredContent=rows)), "list", {})
    assert json.loads(out) == rows


async def test_with_a_text_block_the_text_is_the_result_as_before():
    """The spec has a tool that returns structured content mirror it in a text block: not sent twice."""
    result = CallToolResult(content=[TextContent(type="text", text='{"total": 1}')], structuredContent={"total": 1})
    assert await ConnectionPool._execute_tool_call(_server(result), "list", {}) == '{"total": 1}'


async def test_structured_content_beside_an_image_only_comes_first():
    result = CallToolResult(
        content=[ImageContent(type="image", data="aGk=", mimeType="image/png")], structuredContent={"chart": "sales"},
    )
    out = await ConnectionPool._execute_tool_call(_server(result), "plot", {})
    assert out.splitlines() == ['{"chart": "sales"}', "[binary: image/png]"]


async def test_a_structured_only_error_says_what_it_was():
    result = CallToolResult(content=[], structuredContent={"code": "not_found", "id": "IN-9"}, isError=True)
    out = json.loads(await ConnectionPool._execute_tool_call(_server(result), "get", {}))
    assert json.loads(out["error"]) == {"code": "not_found", "id": "IN-9"}


async def test_secrets_in_structured_content_are_masked_as_in_text():
    result = CallToolResult(content=[], structuredContent={"note": "password=hunter2"})
    out = await ConnectionPool._execute_tool_call(_server(result), "get", {})
    assert "hunter2" not in out and "[REDACTED]" in out


async def test_an_empty_text_block_beside_structured_content_does_not_hide_it():
    result = CallToolResult(content=[TextContent(type="text", text="")], structuredContent={"total": 3})
    assert json.loads(await ConnectionPool._execute_tool_call(_server(result), "count", {})) == {"total": 3}


async def test_a_stdio_servers_structured_only_answer_reaches_the_model_too(monkeypatch):
    """stdio is the proxy's default transport: its calls run in a per-call sandbox (routes._execute_call)."""
    from contextlib import asynccontextmanager
    from uuid import uuid4

    from surogates.mcp_proxy import routes as routes_module

    rows = {"drops": [{"id": "D-3", "depot": "Upper North"}]}

    class Session:
        async def call_tool(self, name, arguments=None, meta=None):
            return CallToolResult(content=[], structuredContent=rows)

    class Sandbox:
        def __init__(self, **kwargs):
            pass

        @asynccontextmanager
        async def mcp_session(self):
            yield Session()

    monkeypatch.setattr(routes_module, "MCPCallSandbox", Sandbox)
    text, outcome = await routes_module._execute_call(
        pool=SimpleNamespace(utility_of=lambda *args: None), org_id=uuid4(), user_id=uuid4(), agent_id="a",
        server_config={"transport": "stdio", "command": "srv"},
        clean_tool_name="mcp__lw__list_drops", original_tool="list_drops", arguments={}, meta=None,
    )
    assert outcome == "success" and json.loads(text) == rows
