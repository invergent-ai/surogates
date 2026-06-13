"""Tests for surogates.sandbox.executor_server — the persistent in-pod daemon."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

import httpx
import pytest
import uvicorn

from surogates.sandbox import executor_server

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI_PATH = os.path.join(REPO_ROOT, "images", "sandbox", "tool-executor")


# ---------------------------------------------------------------------------
# workspace_mounted
# ---------------------------------------------------------------------------


class TestWorkspaceMounted:
    def test_fuse_mount_detected(self, tmp_path):
        mounts = tmp_path / "mounts"
        mounts.write_text(
            "overlay / overlay rw 0 0\n"
            "geesefs /workspace fuse.geesefs rw,nosuid,nodev 0 0\n"
        )
        assert executor_server.workspace_mounted("/workspace", str(mounts)) is True

    def test_plain_bind_mount_is_not_enough(self, tmp_path):
        # The emptyDir volumeMount makes /workspace a mount point with a
        # non-FUSE fstype — that must NOT count as "geesefs is up".
        mounts = tmp_path / "mounts"
        mounts.write_text(
            "overlay / overlay rw 0 0\n"
            "/dev/sda1 /workspace ext4 rw 0 0\n"
        )
        assert executor_server.workspace_mounted("/workspace", str(mounts)) is False

    def test_no_entry(self, tmp_path):
        mounts = tmp_path / "mounts"
        mounts.write_text("overlay / overlay rw 0 0\n")
        assert executor_server.workspace_mounted("/workspace", str(mounts)) is False

    def test_trailing_slash_normalized(self, tmp_path):
        mounts = tmp_path / "mounts"
        mounts.write_text("geesefs /workspace fuse.geesefs rw 0 0\n")
        assert executor_server.workspace_mounted("/workspace/", str(mounts)) is True

    def test_unreadable_mounts_file(self):
        assert executor_server.workspace_mounted("/workspace", "/nonexistent") is False


# ---------------------------------------------------------------------------
# run_tool — child-side dispatch
# ---------------------------------------------------------------------------


class FakeRegistry:
    """Stands in for ToolRegistry; behaviors keyed by tool name."""

    async def dispatch(self, name, args, **kwargs):
        if name == "missing":
            raise KeyError(name)
        if name == "boom":
            raise RuntimeError("boom")
        if name == "slow":
            await asyncio.sleep(float(args.get("seconds", 1.0)))
            return json.dumps({"ok": True, "slept": True})
        if name == "spin":  # CPU-bound, never yields
            deadline = time.monotonic() + float(args.get("seconds", 1.0))
            while time.monotonic() < deadline:
                pass
            return json.dumps({"ok": True, "spun": True})
        if name == "die":  # simulates a native-code crash
            os._exit(7)
        return json.dumps({
            "ok": True,
            "echo": args,
            "kwargs_has_workspace": "workspace_path" in kwargs,
        })


@pytest.fixture()
def fake_registry(monkeypatch):
    registry = FakeRegistry()
    monkeypatch.setattr(executor_server, "_REGISTRY", registry)
    return registry


class TestRunTool:
    def test_dispatches_through_registry(self, fake_registry):
        result = json.loads(executor_server.run_tool("echo", {"a": 1}, "/ws"))
        assert result["ok"] is True
        assert result["echo"] == {"a": 1}
        assert result["kwargs_has_workspace"] is True

    def test_unknown_tool(self, fake_registry):
        result = json.loads(executor_server.run_tool("missing", {}, "/ws"))
        assert result == {
            "exit_code": 1,
            "output": "",
            "error": "Unknown tool: missing",
        }

    def test_handler_exception(self, fake_registry):
        result = json.loads(executor_server.run_tool("boom", {}, "/ws"))
        assert result == {"exit_code": 1, "output": "", "error": "boom"}

    def test_checkpoint_branch(self, monkeypatch):
        class FakeMgr:
            def __init__(self, enabled):
                pass

            def latest_hash(self, workspace):
                return "abc123"

        monkeypatch.setattr(
            "surogates.tools.utils.checkpoint_manager.CheckpointManager", FakeMgr,
        )
        result = json.loads(
            executor_server.run_tool("_checkpoint", {"action": "latest_hash"}, "/ws"),
        )
        assert result == {"success": True, "hash": "abc123"}

    def test_code_branch(self, monkeypatch):
        monkeypatch.setattr(
            "surogates.coding_agents.pod_runner.dispatch",
            lambda args: {"ok": True, "action": args.get("action")},
        )
        result = json.loads(
            executor_server.run_tool("_code", {"action": "status"}, "/ws"),
        )
        assert result == {"ok": True, "action": "status"}


# ---------------------------------------------------------------------------
# execute_in_child — fork runner
# ---------------------------------------------------------------------------


class TestExecuteInChild:
    async def test_result_roundtrip(self, fake_registry):
        result = json.loads(
            await executor_server.execute_in_child("echo", {"x": 2}, "/ws", timeout=10),
        )
        assert result["ok"] is True
        assert result["echo"] == {"x": 2}

    async def test_timeout_kills_child(self, fake_registry):
        start = time.monotonic()
        result = json.loads(
            await executor_server.execute_in_child(
                "slow", {"seconds": 10}, "/ws", timeout=0.3,
            ),
        )
        elapsed = time.monotonic() - start
        assert result["timed_out"] is True
        assert result["exit_code"] == -1
        assert "timed out" in result["stderr"].lower()
        assert elapsed < 5, f"timeout did not kill promptly: {elapsed:.1f}s"

    async def test_child_abnormal_death(self, fake_registry):
        result = json.loads(
            await executor_server.execute_in_child("die", {}, "/ws", timeout=10),
        )
        assert result["exit_code"] == 1
        assert "died" in result["error"]
        assert "7" in result["error"]


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


def _make_client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://daemon")


@pytest.fixture()
def mounted_mounts_file(tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text("geesefs /workspace fuse.geesefs rw 0 0\n")
    return str(mounts)


@pytest.fixture()
def app(fake_registry, mounted_mounts_file):
    return executor_server.create_app(
        token="secret-token",
        workspace="/workspace",
        mounts_path=mounted_mounts_file,
    )


AUTH = {"Authorization": "Bearer secret-token"}


class TestHttpLayer:
    async def test_execute_requires_token(self, app):
        async with _make_client(app) as client:
            resp = await client.post("/execute", json={"name": "echo", "args": {}})
            assert resp.status_code == 401
            resp = await client.post(
                "/execute",
                json={"name": "echo", "args": {}},
                headers={"Authorization": "Bearer wrong"},
            )
            assert resp.status_code == 401

    async def test_execute_happy_path(self, app):
        async with _make_client(app) as client:
            resp = await client.post(
                "/execute",
                json={"name": "echo", "args": {"a": 1}},
                headers=AUTH,
            )
            assert resp.status_code == 200
            body = json.loads(resp.text)
            assert body["ok"] is True
            assert body["echo"] == {"a": 1}

    async def test_execute_missing_name(self, app):
        async with _make_client(app) as client:
            resp = await client.post("/execute", json={"args": {}}, headers=AUTH)
            assert resp.status_code == 200
            assert json.loads(resp.text)["error"] == "No tool name provided"

    async def test_execute_body_timeout(self, app):
        async with _make_client(app) as client:
            start = time.monotonic()
            resp = await client.post(
                "/execute",
                json={"name": "slow", "args": {"seconds": 10}, "timeout": 0.3},
                headers=AUTH,
            )
            assert json.loads(resp.text)["timed_out"] is True
            assert time.monotonic() - start < 5

    async def test_healthz_unauthenticated_when_mounted(self, app):
        async with _make_client(app) as client:
            resp = await client.get("/healthz")
            assert resp.status_code == 200

    async def test_healthz_503_when_not_mounted(self, fake_registry, tmp_path):
        mounts = tmp_path / "mounts"
        mounts.write_text("/dev/sda1 /workspace ext4 rw 0 0\n")
        app = executor_server.create_app(
            token="secret-token", workspace="/workspace", mounts_path=str(mounts),
        )
        async with _make_client(app) as client:
            resp = await client.get("/healthz")
            assert resp.status_code == 503

    async def test_concurrent_executes_overlap(self, app):
        async with _make_client(app) as client:
            start = time.monotonic()
            await asyncio.gather(
                client.post(
                    "/execute",
                    json={"name": "slow", "args": {"seconds": 0.6}},
                    headers=AUTH,
                ),
                client.post(
                    "/execute",
                    json={"name": "slow", "args": {"seconds": 0.6}},
                    headers=AUTH,
                ),
            )
            elapsed = time.monotonic() - start
            assert elapsed < 1.1, f"requests serialized: {elapsed:.2f}s"

    async def test_cpu_bound_tool_does_not_block_healthz(self, app):
        async with _make_client(app) as client:
            spin = asyncio.create_task(
                client.post(
                    "/execute",
                    json={"name": "spin", "args": {"seconds": 1.5}},
                    headers=AUTH,
                ),
            )
            await asyncio.sleep(0.1)
            start = time.monotonic()
            resp = await client.get("/healthz")
            elapsed = time.monotonic() - start
            assert resp.status_code == 200
            assert elapsed < 0.5, f"healthz starved by CPU-bound tool: {elapsed:.2f}s"
            await spin

    async def test_child_death_keeps_daemon_serving(self, app):
        async with _make_client(app) as client:
            resp = await client.post(
                "/execute", json={"name": "die", "args": {}}, headers=AUTH,
            )
            assert "died" in json.loads(resp.text)["error"]
            # Daemon still serves after the child crash.
            resp = await client.post(
                "/execute", json={"name": "echo", "args": {}}, headers=AUTH,
            )
            assert json.loads(resp.text)["ok"] is True


# ---------------------------------------------------------------------------
# Thin-client CLI against a live server
# ---------------------------------------------------------------------------


class TestThinClientCli:
    async def test_cli_forwards_to_daemon(self, app):
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
        server = uvicorn.Server(config)
        serve_task = asyncio.create_task(server.serve())
        try:
            while not server.started:
                await asyncio.sleep(0.01)
            port = server.servers[0].sockets[0].getsockname()[1]

            env = {
                **os.environ,
                "TOOL_EXECUTOR_PORT": str(port),
                "TOOL_EXECUTOR_TOKEN": "secret-token",
            }
            proc = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, CLI_PATH, "echo", json.dumps({"b": 2})],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert proc.returncode == 0, proc.stderr
            body = json.loads(proc.stdout)
            assert body["ok"] is True
            assert body["echo"] == {"b": 2}
        finally:
            server.should_exit = True
            await serve_task


class TestHealthzRequireFuse:
    async def test_healthz_ok_without_fuse_when_not_required(
        self, fake_registry, tmp_path
    ):
        # A mounts file with no FUSE entry at /workspace.
        mounts = tmp_path / "mounts"
        mounts.write_text("overlay / overlay rw 0 0\n")
        app = executor_server.create_app(
            token="t",
            workspace="/workspace",
            mounts_path=str(mounts),
            require_fuse=False,
        )
        async with _make_client(app) as client:
            resp = await client.get("/healthz")
            assert resp.status_code == 200

    async def test_healthz_503_without_fuse_when_required(
        self, fake_registry, tmp_path
    ):
        mounts = tmp_path / "mounts"
        mounts.write_text("overlay / overlay rw 0 0\n")
        app = executor_server.create_app(
            token="t",
            workspace="/workspace",
            mounts_path=str(mounts),
            require_fuse=True,
        )
        async with _make_client(app) as client:
            resp = await client.get("/healthz")
            assert resp.status_code == 503
