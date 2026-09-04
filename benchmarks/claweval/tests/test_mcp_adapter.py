import json
import pathlib

import httpx
import pytest
import respx

from claweval_bench.mcp_adapter import DispatchLog, forward, load_task_tools

ENDPOINTS = {
    "config_list": {"url": "http://localhost:9111/config/integrations",
                    "method": "POST"},
}


@pytest.fixture
def log(tmp_path):
    return DispatchLog(tmp_path / "dispatches.jsonl"), tmp_path / "dispatches.jsonl"


def read_log(path: pathlib.Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines()]


@respx.mock
async def test_forward_success_returns_json_body_and_logs(log):
    dispatch_log, path = log
    respx.post("http://localhost:9111/config/integrations").mock(
        return_value=httpx.Response(200, json={"items": [1, 2]}),
    )
    async with httpx.AsyncClient() as http:
        text = await forward("config_list", {"status": "active"},
                             ENDPOINTS, dispatch_log, http)
    assert json.loads(text) == {"items": [1, 2]}
    records = read_log(path)
    assert len(records) == 1
    assert records[0]["tool_name"] == "config_list"
    assert records[0]["request_body"] == {"status": "active"}
    assert records[0]["response_status"] == 200


@respx.mock
async def test_forward_http_error_stays_in_band(log):
    dispatch_log, path = log
    respx.post("http://localhost:9111/config/integrations").mock(
        return_value=httpx.Response(500, json={"error": "boom"}),
    )
    async with httpx.AsyncClient() as http:
        text = await forward("config_list", {}, ENDPOINTS, dispatch_log, http)
    # Upstream serves the error body to the model, flagged via status only.
    assert json.loads(text) == {"error": "boom"}
    assert read_log(path)[0]["response_status"] == 500


async def test_forward_unknown_tool_is_in_band_error(log):
    dispatch_log, path = log
    async with httpx.AsyncClient() as http:
        text = await forward("nope", {}, ENDPOINTS, dispatch_log, http)
    assert text.startswith("Error: unknown tool")
    assert read_log(path)[0]["response_status"] == 404


@respx.mock
async def test_forward_unreachable_service_is_in_band_error(log):
    dispatch_log, path = log
    respx.post("http://localhost:9111/config/integrations").mock(
        side_effect=httpx.ConnectError("refused"),
    )
    async with httpx.AsyncClient() as http:
        text = await forward("config_list", {}, ENDPOINTS, dispatch_log, http)
    assert text.startswith("Error: service unreachable")
    assert read_log(path)[0]["response_status"] == 599


def test_load_task_tools_parses_yaml(tmp_path):
    (tmp_path / "task.yaml").write_text(
        "tools:\n"
        "  - name: t1\n"
        "    description: d\n"
        "    input_schema: {type: object, properties: {}}\n"
        "tool_endpoints:\n"
        "  - tool_name: t1\n"
        "    url: http://localhost:1/x\n",
    )
    tools, endpoints = load_task_tools(tmp_path / "task.yaml")
    assert tools[0]["name"] == "t1"
    assert endpoints["t1"] == {"url": "http://localhost:1/x", "method": "POST"}
