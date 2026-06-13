"""Tests for surogates.sandbox._executor_client.ExecutorHTTPClient."""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web

from surogates.sandbox._executor_client import ExecutorHTTPClient
from surogates.sandbox.base import SandboxUnavailableError


async def _serve(handler) -> tuple[web.AppRunner, int]:
    app = web.Application()
    app.router.add_post("/execute", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, shutdown_timeout=0.5)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


class TestExecute:
    async def test_passthrough_and_auth_header(self):
        seen = {}

        async def handler(request):
            seen["auth"] = request.headers.get("Authorization")
            seen["body"] = await request.json()
            return web.Response(text='{"ok": true}', content_type="application/json")

        runner, port = await _serve(handler)
        client = ExecutorHTTPClient()
        try:
            result = await client.execute(
                host="127.0.0.1", port=port, token="tok-abc",
                name="list_files", args_str='{"pattern": "*"}', timeout=5,
            )
            assert json.loads(result) == {"ok": True}
            assert seen["auth"] == "Bearer tok-abc"
            assert seen["body"] == {
                "name": "list_files", "args": {"pattern": "*"}, "timeout": 5,
            }
        finally:
            await runner.cleanup()
            await client.aclose()

    async def test_bad_json_args_become_empty_dict(self):
        seen = {}

        async def handler(request):
            seen["body"] = await request.json()
            return web.Response(text="{}", content_type="application/json")

        runner, port = await _serve(handler)
        client = ExecutorHTTPClient()
        try:
            await client.execute(
                host="127.0.0.1", port=port, token="t",
                name="x", args_str="not-json", timeout=5,
            )
            assert seen["body"]["args"] == {}
        finally:
            await runner.cleanup()
            await client.aclose()

    async def test_401_raises_unavailable(self):
        async def handler(request):
            return web.Response(status=401, text="unauthorized")

        runner, port = await _serve(handler)
        client = ExecutorHTTPClient()
        try:
            with pytest.raises(SandboxUnavailableError):
                await client.execute(
                    host="127.0.0.1", port=port, token="t",
                    name="x", args_str="{}", timeout=5,
                )
        finally:
            await runner.cleanup()
            await client.aclose()

    async def test_500_returns_error_result(self):
        async def handler(request):
            return web.Response(status=500, text="kaboom")

        runner, port = await _serve(handler)
        client = ExecutorHTTPClient()
        try:
            result = json.loads(await client.execute(
                host="127.0.0.1", port=port, token="t",
                name="x", args_str="{}", timeout=5,
            ))
            assert result["exit_code"] == -1
            assert "500" in result["stderr"]
            assert result["timed_out"] is False
        finally:
            await runner.cleanup()
            await client.aclose()

    async def test_connection_refused_raises_unavailable(self):
        client = ExecutorHTTPClient()
        try:
            with pytest.raises(SandboxUnavailableError):
                # Nothing listens on port 1.
                await client.execute(
                    host="127.0.0.1", port=1, token="t",
                    name="x", args_str="{}", timeout=5,
                )
        finally:
            await client.aclose()

    async def test_timeout_returns_timed_out(self):
        async def handler(request):
            await asyncio.sleep(30)
            return web.Response(text="{}")

        runner, port = await _serve(handler)
        client = ExecutorHTTPClient()
        try:
            # total budget = timeout + 5; -4 → 1s so the test is fast.
            result = json.loads(await client.execute(
                host="127.0.0.1", port=port, token="t",
                name="x", args_str="{}", timeout=-4,
            ))
            assert result["timed_out"] is True
        finally:
            await runner.cleanup()
            await client.aclose()
