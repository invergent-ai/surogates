"""Serve one claw-eval task's tools over MCP (streamable HTTP).

The surogates harness consumes tools from registered MCP servers via its
mcp-proxy. Upstream claw-eval instead dispatches tool calls in-process
straight to mock-service HTTP endpoints. This adapter bridges the two:
it reads a ``task.yaml``, exposes each declared tool over MCP with the
exact upstream schema, and forwards calls with the exact upstream
semantics (``request(method, url, json=arguments)``; the result content is
the JSON body; HTTP >= 400 marks the result as an error).

Every forwarded call is also appended to a JSONL dispatch log so grading
sees the same ``ToolDispatch`` evidence the upstream harness records. The
``tool_use_id`` is synthesized: the LLM-side call id does not cross the
MCP boundary, and no vendored grader keys on it.

Run as a subprocess, one task at a time:

    python -m claweval_bench.mcp_adapter --task-yaml tasks/T028_x/task.yaml \
        --port 8321 --dispatch-log runs/r1/tasks/T028_x/dispatches.jsonl
"""
from __future__ import annotations

import argparse
import contextlib
import json
import pathlib
import time
import uuid
from typing import Any

import httpx
import mcp.types as types
import uvicorn
import yaml
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

HEALTH_PATH = "/healthz"
MCP_PATH = "/mcp"


def load_task_tools(task_yaml: pathlib.Path) -> tuple[list[dict], dict[str, dict]]:
    """Return (tool specs, tool_name -> {url, method}) from a task.yaml.

    Parsed with plain yaml rather than claw_eval's models so the adapter
    subprocess starts fast and works even in a venv without the vendored
    package (the runner, not the adapter, needs the graders).
    """
    data = yaml.safe_load(task_yaml.read_text())
    tools = data.get("tools") or []
    endpoints = {
        e["tool_name"]: {"url": e["url"], "method": e.get("method", "POST")}
        for e in (data.get("tool_endpoints") or [])
    }
    return tools, endpoints


class DispatchLog:
    def __init__(self, path: pathlib.Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict[str, Any]) -> None:
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


async def forward(
    name: str,
    arguments: dict,
    endpoints: dict[str, dict],
    log: DispatchLog,
    http: httpx.AsyncClient,
) -> str:
    """Forward one tool call with upstream dispatch semantics.

    Returns the in-band result text: JSON body on success, an ``Error:``
    string otherwise. Upstream never surfaces a protocol error -- the
    model must see the failure and adapt, so ours must too.
    """
    endpoint = endpoints.get(name)
    if endpoint is None:
        log.append({
            "tool_use_id": uuid.uuid4().hex,
            "tool_name": name,
            "endpoint_url": "",
            "request_body": arguments,
            "response_status": 404,
            "response_body": {"error": f"unknown tool '{name}'"},
            "latency_ms": 0.0,
        })
        return f"Error: unknown tool '{name}'"

    t0 = time.monotonic()
    try:
        resp = await http.request(
            method=endpoint["method"], url=endpoint["url"], json=arguments,
        )
        latency_ms = (time.monotonic() - t0) * 1000
        try:
            body: Any = resp.json()
        except ValueError:
            body = {"raw": resp.text[:2000]}
        log.append({
            "tool_use_id": uuid.uuid4().hex,
            "tool_name": name,
            "endpoint_url": endpoint["url"],
            "request_body": arguments,
            "response_status": resp.status_code,
            "response_body": body,
            "latency_ms": latency_ms,
        })
        return json.dumps(body, ensure_ascii=False)
    except httpx.HTTPError as exc:
        log.append({
            "tool_use_id": uuid.uuid4().hex,
            "tool_name": name,
            "endpoint_url": endpoint["url"],
            "request_body": arguments,
            "response_status": 599,
            "response_body": {"error": str(exc)},
            "latency_ms": (time.monotonic() - t0) * 1000,
        })
        return f"Error: service unreachable: {exc}"


def build_server(
    tools: list[dict],
    endpoints: dict[str, dict],
    log: DispatchLog,
) -> Server:
    server: Server = Server("claweval-task")
    specs = [
        types.Tool(
            name=t["name"],
            description=t.get("description", ""),
            inputSchema=t.get("input_schema") or {"type": "object", "properties": {}},
        )
        for t in tools
    ]
    http = httpx.AsyncClient(timeout=30.0)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return specs

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        text = await forward(name, arguments, endpoints, log, http)
        return [types.TextContent(type="text", text=text)]

    return server


def build_app(server: Server) -> Starlette:
    manager = StreamableHTTPSessionManager(
        app=server, event_store=None, json_response=True, stateless=True,
    )

    async def healthz(_request: Any) -> JSONResponse:
        return JSONResponse({"ok": True})

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        async with manager.run():
            yield

    return Starlette(
        routes=[
            Route(HEALTH_PATH, healthz),
            Mount(MCP_PATH, app=manager.handle_request),
        ],
        lifespan=lifespan,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="claweval-mcp-adapter")
    parser.add_argument("--task-yaml", required=True)
    parser.add_argument("--port", type=int, default=8321)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--dispatch-log", required=True)
    args = parser.parse_args(argv)

    tools, endpoints = load_task_tools(pathlib.Path(args.task_yaml))
    log = DispatchLog(pathlib.Path(args.dispatch_log))
    app = build_app(build_server(tools, endpoints, log))
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
