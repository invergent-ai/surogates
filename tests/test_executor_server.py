"""Tests for surogates.sandbox.executor_server — the persistent in-pod daemon."""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from surogates.sandbox import executor_server


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
