"""Tests for surogates.sandbox.process.ProcessSandbox and surogates.sandbox.pool.SandboxPool."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from surogates.sandbox.base import SandboxSpec, SandboxStatus
from surogates.sandbox.pool import SandboxPool
from surogates.sandbox.process import ProcessSandbox


# =========================================================================
# ProcessSandbox
# =========================================================================


class TestProcessSandbox:
    """Test the subprocess-based sandbox backend."""

    @pytest.mark.asyncio
    async def test_provision_creates_sandbox(self):
        sandbox = ProcessSandbox()
        spec = SandboxSpec()
        sandbox_id = await sandbox.provision(spec)
        assert isinstance(sandbox_id, str)
        assert len(sandbox_id) == 32  # UUID hex
        status = await sandbox.status(sandbox_id)
        assert status == SandboxStatus.RUNNING
        await sandbox.destroy(sandbox_id)

    @pytest.mark.asyncio
    async def test_execute_runs_command(self):
        sandbox = ProcessSandbox()
        spec = SandboxSpec()
        sandbox_id = await sandbox.provision(spec)

        result_json = await sandbox.execute(sandbox_id, "echo", "hello")
        result = json.loads(result_json)
        assert result["exit_code"] == 0
        assert result["timed_out"] is False
        # echo reads stdin but just prints a newline; the important thing
        # is that the command ran successfully.
        await sandbox.destroy(sandbox_id)

    @pytest.mark.asyncio
    async def test_execute_respects_timeout(self):
        sandbox = ProcessSandbox()
        spec = SandboxSpec(timeout=1)  # 1-second timeout
        sandbox_id = await sandbox.provision(spec)

        # Use python to sleep -- exec replaces the shell so kill is clean.
        result_json = await sandbox.execute(
            sandbox_id,
            "python3",
            "import time; time.sleep(60)",
        )
        result = json.loads(result_json)
        assert result["timed_out"] is True
        await sandbox.destroy(sandbox_id)

    @pytest.mark.asyncio
    async def test_execute_command_not_found(self):
        sandbox = ProcessSandbox()
        spec = SandboxSpec()
        sandbox_id = await sandbox.provision(spec)

        result_json = await sandbox.execute(
            sandbox_id, "/nonexistent/command/abc123", ""
        )
        result = json.loads(result_json)
        assert result["exit_code"] == -1
        assert "not found" in result["stderr"] or "No such file" in result["stderr"]
        await sandbox.destroy(sandbox_id)

    @pytest.mark.asyncio
    async def test_destroy_removes_sandbox(self):
        sandbox = ProcessSandbox()
        spec = SandboxSpec()
        sandbox_id = await sandbox.provision(spec)
        await sandbox.destroy(sandbox_id)

        status = await sandbox.status(sandbox_id)
        assert status == SandboxStatus.TERMINATED

    @pytest.mark.asyncio
    async def test_status_running_vs_terminated(self):
        sandbox = ProcessSandbox()
        spec = SandboxSpec()
        sandbox_id = await sandbox.provision(spec)
        assert await sandbox.status(sandbox_id) == SandboxStatus.RUNNING
        await sandbox.destroy(sandbox_id)
        assert await sandbox.status(sandbox_id) == SandboxStatus.TERMINATED

    @pytest.mark.asyncio
    async def test_execute_unknown_sandbox_raises(self):
        sandbox = ProcessSandbox()
        with pytest.raises(ValueError, match="Unknown sandbox"):
            await sandbox.execute("nonexistent", "echo", "")

    @pytest.mark.asyncio
    async def test_destroy_unknown_sandbox_no_error(self):
        sandbox = ProcessSandbox()
        # Should not raise.
        await sandbox.destroy("nonexistent")


# =========================================================================
# SandboxPool
# =========================================================================


class TestSandboxPool:
    """Test session-aware sandbox pooling."""

    @pytest.mark.asyncio
    async def test_ensure_provisions_on_first_call(self):
        backend = ProcessSandbox()
        pool = SandboxPool(backend)
        spec = SandboxSpec()

        sandbox_id = await pool.ensure("session-1", spec)
        assert isinstance(sandbox_id, str)
        assert len(sandbox_id) == 32
        await pool.destroy_for_session("session-1")

    @pytest.mark.asyncio
    async def test_ensure_reuses_on_second_call(self):
        backend = ProcessSandbox()
        pool = SandboxPool(backend)
        spec = SandboxSpec()

        id1 = await pool.ensure("session-1", spec)
        id2 = await pool.ensure("session-1", spec)
        assert id1 == id2
        await pool.destroy_for_session("session-1")

    @pytest.mark.asyncio
    async def test_different_sessions_get_different_sandboxes(self):
        backend = ProcessSandbox()
        pool = SandboxPool(backend)
        spec = SandboxSpec()

        id1 = await pool.ensure("session-1", spec)
        id2 = await pool.ensure("session-2", spec)
        assert id1 != id2
        await pool.destroy_for_session("session-1")
        await pool.destroy_for_session("session-2")

    @pytest.mark.asyncio
    async def test_destroy_for_session_cleans_up(self):
        backend = ProcessSandbox()
        pool = SandboxPool(backend)
        spec = SandboxSpec()

        sandbox_id = await pool.ensure("session-1", spec)
        await pool.destroy_for_session("session-1")

        # The sandbox should no longer be running.
        status = await backend.status(sandbox_id)
        assert status == SandboxStatus.TERMINATED

    @pytest.mark.asyncio
    async def test_execute_raises_without_provisioning(self):
        backend = ProcessSandbox()
        pool = SandboxPool(backend)

        with pytest.raises(ValueError, match="No sandbox"):
            await pool.execute("no-such-session", "echo", "hello")

    @pytest.mark.asyncio
    async def test_destroy_all(self):
        backend = ProcessSandbox()
        pool = SandboxPool(backend)
        spec = SandboxSpec()

        id1 = await pool.ensure("s1", spec)
        id2 = await pool.ensure("s2", spec)

        await pool.destroy_all()

        assert await backend.status(id1) == SandboxStatus.TERMINATED
        assert await backend.status(id2) == SandboxStatus.TERMINATED
