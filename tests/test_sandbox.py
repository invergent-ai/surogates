"""Process sandbox execution and pool lifecycle workflows."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from surogates.sandbox.base import SandboxSpec, SandboxStatus
from surogates.sandbox.pool import SandboxPool
from surogates.sandbox.process import ProcessSandbox


class TestProcessSandbox:
    """Test the subprocess-based sandbox backend."""


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


class TestReleaseThenDestroy:
    """Detaching is fast and ordered; deleting the pod is neither.

    Pod deletion is a round trip to the cluster and used to sit between
    the agent's last word and SESSION_COMPLETE, so the user watched a
    busy indicator through it. Splitting the two lets the slow half move
    off that path without loosening the guarantee that matters: once a
    session is released, no later turn can resolve it to a pod that is
    about to disappear.
    """

    @pytest.mark.asyncio
    async def test_release_detaches_before_the_pod_is_gone(self):
        backend = ProcessSandbox()
        pool = SandboxPool(backend)
        sandbox_id = await pool.ensure("session-r1", SandboxSpec())

        released = await pool.release_for_session("session-r1")
        assert released == sandbox_id

        # Detached: a call for this session can no longer reach it, even
        # though the sandbox itself still exists.
        with pytest.raises(ValueError):
            await pool.execute("session-r1", "terminal", "{}")
        assert await backend.status(sandbox_id) == "running"

        await pool.destroy_released(released, "session-r1")
        assert await backend.status(sandbox_id) != "running"

    @pytest.mark.asyncio
    async def test_ensure_after_release_provisions_a_fresh_sandbox(self):
        """The race the ordering exists to prevent.

        A turn starting while the previous teardown is still in flight
        must get its own sandbox, never the one being deleted.
        """
        backend = ProcessSandbox()
        pool = SandboxPool(backend)
        first = await pool.ensure("session-r2", SandboxSpec())
        released = await pool.release_for_session("session-r2")

        second = await pool.ensure("session-r2", SandboxSpec())
        assert second != first, "reused a sandbox that was being torn down"

        await pool.destroy_released(released, "session-r2")
        # Tearing down the old one must not disturb the live one.
        assert await backend.status(second) == "running"
        await pool.destroy_for_session("session-r2")

    @pytest.mark.asyncio
    async def test_destroy_for_session_still_works_end_to_end(self):
        """Other callers (shutdown, destroy_all) keep the combined form."""
        backend = ProcessSandbox()
        pool = SandboxPool(backend)
        sandbox_id = await pool.ensure("session-r3", SandboxSpec())
        await pool.destroy_for_session("session-r3")
        assert await backend.status(sandbox_id) != "running"
        with pytest.raises(ValueError):
            await pool.execute("session-r3", "terminal", "{}")


class TestCompletionDoesNotWaitForTeardown:
    """SESSION_COMPLETE must not sit behind a pod deletion.

    Measured over a month of production sessions: with neither a sandbox
    nor a turn summary the gap from the agent's last response to
    SESSION_COMPLETE is 0.24s at p50; sessions that used a sandbox reach
    32s at p90. The pod delete was on that path.
    """

    @pytest.mark.asyncio
    async def test_slow_pod_delete_does_not_delay_release(self):
        import time

        class _SlowBackend(ProcessSandbox):
            destroyed = False

            async def destroy(self, sandbox_id):  # noqa: D102
                await asyncio.sleep(0.6)
                _SlowBackend.destroyed = True
                return await super().destroy(sandbox_id)

        backend = _SlowBackend()
        pool = SandboxPool(backend)
        await pool.ensure("session-slow", SandboxSpec())

        # The half that stays on the critical path.
        t0 = time.monotonic()
        released = await pool.release_for_session("session-slow")
        detach_s = time.monotonic() - t0

        assert detach_s < 0.1, f"detach took {detach_s:.2f}s -- it is on the hot path"
        assert not _SlowBackend.destroyed, "pod deleted during detach"

        # And the slow half still completes when awaited (the drain).
        await pool.destroy_released(released, "session-slow")
        assert _SlowBackend.destroyed
