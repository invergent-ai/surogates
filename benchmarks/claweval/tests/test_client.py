import json

import httpx
import pytest
import respx

from claweval_bench.client import DEFAULT_EXCLUDED_TOOLS, HarnessClient

BASE = "http://localhost:8000"


def _mock_create() -> respx.Route:
    return respx.post(f"{BASE}/v1/api/sessions").mock(
        return_value=httpx.Response(200, json={"id": "sess-1"}),
    )


@respx.mock
async def test_create_session_excludes_native_tools_by_default():
    route = _mock_create()
    async with HarnessClient(BASE, "tok", "agent-1") as client:
        assert await client.create_session() == "sess-1"
    body = json.loads(route.calls[0].request.content)
    assert body["config"]["excluded_tools"] == list(DEFAULT_EXCLUDED_TOOLS)


@respx.mock
async def test_create_session_custom_exclusions():
    route = _mock_create()
    async with HarnessClient(
        BASE, "tok", "agent-1", excluded_tools=["web_search"],
    ) as client:
        await client.create_session()
    body = json.loads(route.calls[0].request.content)
    assert body["config"]["excluded_tools"] == ["web_search"]


@respx.mock
async def test_create_session_empty_exclusions_sends_no_key():
    route = _mock_create()
    async with HarnessClient(
        BASE, "tok", "agent-1", excluded_tools=[],
    ) as client:
        await client.create_session()
    body = json.loads(route.calls[0].request.content)
    assert "excluded_tools" not in body["config"]
