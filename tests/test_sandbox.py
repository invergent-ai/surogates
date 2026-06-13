"""Tests for surogates.sandbox.process.ProcessSandbox and surogates.sandbox.pool.SandboxPool."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

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
    async def test_execute_calls_overlap_for_same_session(self):
        """Two concurrent execute() calls must not serialize on the session lock."""

        class SlowBackend:
            async def provision(self, spec):
                return "sb-1"

            async def status(self, sandbox_id):
                return SandboxStatus.RUNNING

            async def execute(self, sandbox_id, name, input):
                await asyncio.sleep(0.3)
                return "{}"

            async def destroy(self, sandbox_id):
                pass

        pool = SandboxPool(SlowBackend())
        await pool.ensure("session-1", SandboxSpec())

        start = time.monotonic()
        await asyncio.gather(
            pool.execute("session-1", "tool_a", "{}"),
            pool.execute("session-1", "tool_b", "{}"),
        )
        elapsed = time.monotonic() - start
        # Serialized: ~0.6s. Concurrent: ~0.3s.
        assert elapsed < 0.5, f"execute() calls serialized: {elapsed:.2f}s"

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


def test_sandbox_spec_has_session_and_workspace_fields():
    from surogates.sandbox.base import SandboxSpec

    # Defaults keep existing call sites working.
    spec = SandboxSpec()
    assert spec.session_id == ""
    assert spec.workspace_path is None

    spec2 = SandboxSpec(session_id="root-123", workspace_path="/tmp/ws")
    assert spec2.session_id == "root-123"
    assert spec2.workspace_path == "/tmp/ws"


def test_sandbox_settings_docker_defaults():
    from surogates.config import SandboxSettings

    s = SandboxSettings()
    # backend literal accepts "docker"
    s2 = SandboxSettings(backend="docker")
    assert s2.backend == "docker"
    assert s.docker_image == "ghcr.io/invergent-ai/surogates-agent-sandbox:latest"
    assert s.docker_executor_port_base == 33000
    assert s.docker_ready_timeout == 60
    assert s.docker_network == "bridge"


class _BackendWithReap:
    def __init__(self):
        self.destroyed_ids = []
        self.reaped_sessions = []

    async def provision(self, spec):
        return "sb-1"

    async def execute(self, sandbox_id, name, input):
        return "{}"

    async def status(self, sandbox_id):
        from surogates.sandbox.base import SandboxStatus
        return SandboxStatus.RUNNING

    async def destroy(self, sandbox_id):
        self.destroyed_ids.append(sandbox_id)

    async def destroy_for_session(self, session_id):
        self.reaped_sessions.append(session_id)


class _BackendNoReap:
    async def provision(self, spec):
        return "sb-1"

    async def execute(self, sandbox_id, name, input):
        return "{}"

    async def status(self, sandbox_id):
        from surogates.sandbox.base import SandboxStatus
        return SandboxStatus.RUNNING

    async def destroy(self, sandbox_id):
        pass


@pytest.mark.asyncio
async def test_pool_destroy_for_session_calls_backend_reap():
    from surogates.sandbox.base import SandboxSpec
    from surogates.sandbox.pool import SandboxPool

    backend = _BackendWithReap()
    pool = SandboxPool(backend)
    await pool.ensure("root-1", SandboxSpec())
    await pool.destroy_for_session("root-1")
    assert backend.destroyed_ids == ["sb-1"]
    assert backend.reaped_sessions == ["root-1"]


@pytest.mark.asyncio
async def test_pool_destroy_for_session_reaps_without_mapping():
    from surogates.sandbox.pool import SandboxPool

    backend = _BackendWithReap()
    pool = SandboxPool(backend)
    # No ensure() — pool has no mapping, but the backend should still reap.
    await pool.destroy_for_session("orphan-1")
    assert backend.destroyed_ids == []
    assert backend.reaped_sessions == ["orphan-1"]


@pytest.mark.asyncio
async def test_pool_destroy_for_session_without_backend_reap_is_noop():
    from surogates.sandbox.pool import SandboxPool

    backend = _BackendNoReap()
    pool = SandboxPool(backend)
    # Backend has no destroy_for_session — must not raise.
    await pool.destroy_for_session("root-1")
