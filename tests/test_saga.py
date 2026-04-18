"""Tests for surogates.governance.saga -- state machine, orchestrator, compensator."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest

from surogates.governance.saga.state_machine import (
    Saga,
    SagaState,
    SagaStateError,
    SagaStep,
    StepState,
)
from surogates.governance.saga.orchestrator import (
    SagaOrchestrator,
    SagaTimeoutError,
)


# =========================================================================
# StepState transitions
# =========================================================================


class TestStepStateTransitions:
    """Verify that valid transitions succeed and invalid ones raise."""

    def test_pending_to_executing(self):
        step = SagaStep(
            step_id="s1", tool_name="write_file",
            tool_call_id="tc1", arguments={},
        )
        step.transition(StepState.EXECUTING)
        assert step.state == StepState.EXECUTING
        assert step.started_at is not None

    def test_executing_to_committed(self):
        step = SagaStep(
            step_id="s1", tool_name="write_file",
            tool_call_id="tc1", arguments={},
            state=StepState.EXECUTING,
        )
        step.transition(StepState.COMMITTED)
        assert step.state == StepState.COMMITTED
        assert step.completed_at is not None

    def test_executing_to_failed(self):
        step = SagaStep(
            step_id="s1", tool_name="terminal",
            tool_call_id="tc1", arguments={},
            state=StepState.EXECUTING,
        )
        step.transition(StepState.FAILED)
        assert step.state == StepState.FAILED

    def test_committed_to_compensating(self):
        step = SagaStep(
            step_id="s1", tool_name="write_file",
            tool_call_id="tc1", arguments={},
            state=StepState.COMMITTED,
        )
        step.transition(StepState.COMPENSATING)
        assert step.state == StepState.COMPENSATING

    def test_compensating_to_compensated(self):
        step = SagaStep(
            step_id="s1", tool_name="write_file",
            tool_call_id="tc1", arguments={},
            state=StepState.COMPENSATING,
        )
        step.transition(StepState.COMPENSATED)
        assert step.state == StepState.COMPENSATED

    def test_compensating_to_compensation_failed(self):
        step = SagaStep(
            step_id="s1", tool_name="write_file",
            tool_call_id="tc1", arguments={},
            state=StepState.COMPENSATING,
        )
        step.transition(StepState.COMPENSATION_FAILED)
        assert step.state == StepState.COMPENSATION_FAILED

    def test_invalid_pending_to_committed_raises(self):
        step = SagaStep(
            step_id="s1", tool_name="write_file",
            tool_call_id="tc1", arguments={},
        )
        with pytest.raises(SagaStateError):
            step.transition(StepState.COMMITTED)

    def test_invalid_failed_to_executing_raises(self):
        step = SagaStep(
            step_id="s1", tool_name="write_file",
            tool_call_id="tc1", arguments={},
            state=StepState.FAILED,
        )
        with pytest.raises(SagaStateError):
            step.transition(StepState.EXECUTING)

    def test_terminal_states_have_no_transitions(self):
        for terminal in (StepState.COMPENSATED, StepState.COMPENSATION_FAILED, StepState.FAILED):
            step = SagaStep(
                step_id="s1", tool_name="x",
                tool_call_id="tc1", arguments={},
                state=terminal,
            )
            with pytest.raises(SagaStateError):
                step.transition(StepState.EXECUTING)


# =========================================================================
# SagaState transitions
# =========================================================================


class TestSagaStateTransitions:

    def test_running_to_compensating(self):
        saga = Saga(saga_id="saga:1", session_id=uuid4())
        saga.transition(SagaState.COMPENSATING)
        assert saga.state == SagaState.COMPENSATING

    def test_running_to_completed(self):
        saga = Saga(saga_id="saga:1", session_id=uuid4())
        saga.transition(SagaState.COMPLETED)
        assert saga.state == SagaState.COMPLETED
        assert saga.completed_at is not None

    def test_compensating_to_escalated(self):
        saga = Saga(
            saga_id="saga:1", session_id=uuid4(),
            state=SagaState.COMPENSATING,
        )
        saga.transition(SagaState.ESCALATED)
        assert saga.state == SagaState.ESCALATED

    def test_completed_is_terminal(self):
        saga = Saga(
            saga_id="saga:1", session_id=uuid4(),
            state=SagaState.COMPLETED,
        )
        with pytest.raises(SagaStateError):
            saga.transition(SagaState.RUNNING)


# =========================================================================
# SagaStep helpers
# =========================================================================


class TestSagaStepHelpers:

    def test_is_compensable_with_checkpoint(self):
        step = SagaStep(
            step_id="s1", tool_name="write_file",
            tool_call_id="tc1", arguments={},
            checkpoint_hash="abc123",
        )
        assert step.is_compensable is True

    def test_is_compensable_with_compensation_tool(self):
        step = SagaStep(
            step_id="s1", tool_name="create_ticket",
            tool_call_id="tc1", arguments={},
            compensation_tool="delete_ticket",
        )
        assert step.is_compensable is True

    def test_not_compensable(self):
        step = SagaStep(
            step_id="s1", tool_name="terminal",
            tool_call_id="tc1", arguments={},
        )
        assert step.is_compensable is False


# =========================================================================
# Saga committed_steps
# =========================================================================


class TestSagaCommittedSteps:

    def test_committed_steps_filters(self):
        saga = Saga(saga_id="saga:1", session_id=uuid4())
        saga.steps = [
            SagaStep(step_id="s1", tool_name="a", tool_call_id="tc1",
                     arguments={}, state=StepState.COMMITTED),
            SagaStep(step_id="s2", tool_name="b", tool_call_id="tc2",
                     arguments={}, state=StepState.FAILED),
            SagaStep(step_id="s3", tool_name="c", tool_call_id="tc3",
                     arguments={}, state=StepState.COMMITTED),
        ]
        committed = saga.committed_steps
        assert len(committed) == 2
        assert committed[0].step_id == "s1"
        assert committed[1].step_id == "s3"

    def test_committed_steps_reversed(self):
        saga = Saga(saga_id="saga:1", session_id=uuid4())
        saga.steps = [
            SagaStep(step_id="s1", tool_name="a", tool_call_id="tc1",
                     arguments={}, state=StepState.COMMITTED),
            SagaStep(step_id="s2", tool_name="b", tool_call_id="tc2",
                     arguments={}, state=StepState.COMMITTED),
        ]
        rev = saga.committed_steps_reversed
        assert rev[0].step_id == "s2"
        assert rev[1].step_id == "s1"


# =========================================================================
# Saga.to_dict
# =========================================================================


class TestSagaToDict:

    def test_serialization(self):
        sid = uuid4()
        saga = Saga(saga_id="saga:1", session_id=sid)
        saga.steps.append(
            SagaStep(step_id="s1", tool_name="write_file",
                     tool_call_id="tc1", arguments={"path": "/a"},
                     state=StepState.COMMITTED),
        )
        d = saga.to_dict()
        assert d["saga_id"] == "saga:1"
        assert d["session_id"] == str(sid)
        assert d["state"] == "running"
        assert len(d["steps"]) == 1
        assert d["steps"][0]["tool_name"] == "write_file"


# =========================================================================
# SagaOrchestrator -- create / add_step
# =========================================================================


class TestOrchestratorCreation:

    def test_create_saga(self):
        orch = SagaOrchestrator()
        sid = uuid4()
        saga = orch.create_saga(sid)
        assert saga.session_id == sid
        assert saga.state == SagaState.RUNNING
        assert orch.get_saga(saga.saga_id) is saga

    def test_add_step(self):
        orch = SagaOrchestrator()
        saga = orch.create_saga(uuid4())
        step = orch.add_step(
            saga.saga_id,
            tool_name="write_file",
            tool_call_id="tc1",
            arguments={"path": "/a"},
            checkpoint_hash="abc",
        )
        assert step.tool_name == "write_file"
        assert step.checkpoint_hash == "abc"
        assert step.state == StepState.PENDING
        assert len(saga.steps) == 1

    def test_add_step_unknown_saga_raises(self):
        orch = SagaOrchestrator()
        with pytest.raises(SagaStateError):
            orch.add_step(
                "saga:nonexistent",
                tool_name="x",
                tool_call_id="tc1",
                arguments={},
            )

    def test_active_sagas(self):
        orch = SagaOrchestrator()
        saga = orch.create_saga(uuid4())
        assert saga in orch.active_sagas
        saga.transition(SagaState.COMPLETED)
        assert saga not in orch.active_sagas


# =========================================================================
# SagaOrchestrator -- execute_step
# =========================================================================


class TestOrchestratorExecuteStep:

    @pytest.mark.asyncio
    async def test_successful_execution(self):
        orch = SagaOrchestrator()
        saga = orch.create_saga(uuid4())
        step = orch.add_step(
            saga.saga_id,
            tool_name="write_file",
            tool_call_id="tc1",
            arguments={},
        )

        async def executor():
            return "ok"

        result = await orch.execute_step(saga.saga_id, step.step_id, executor)
        assert result == "ok"
        assert step.state == StepState.COMMITTED
        assert step.execute_result == "ok"

    @pytest.mark.asyncio
    async def test_failed_execution(self):
        orch = SagaOrchestrator()
        saga = orch.create_saga(uuid4())
        step = orch.add_step(
            saga.saga_id,
            tool_name="terminal",
            tool_call_id="tc1",
            arguments={},
        )

        async def executor():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await orch.execute_step(saga.saga_id, step.step_id, executor)
        assert step.state == StepState.FAILED
        assert step.error == "boom"

    @pytest.mark.asyncio
    async def test_timeout(self):
        orch = SagaOrchestrator()
        saga = orch.create_saga(uuid4())
        step = orch.add_step(
            saga.saga_id,
            tool_name="terminal",
            tool_call_id="tc1",
            arguments={},
            timeout_seconds=1,
        )

        async def slow_executor():
            await asyncio.sleep(10)

        with pytest.raises(SagaTimeoutError):
            await orch.execute_step(saga.saga_id, step.step_id, slow_executor)
        assert step.state == StepState.FAILED

    @pytest.mark.asyncio
    async def test_retry_then_succeed(self):
        orch = SagaOrchestrator(retry_delay=0.01)
        saga = orch.create_saga(uuid4())
        step = orch.add_step(
            saga.saga_id,
            tool_name="write_file",
            tool_call_id="tc1",
            arguments={},
            max_retries=1,
        )

        call_count = 0

        async def flaky_executor():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient")
            return "recovered"

        result = await orch.execute_step(saga.saga_id, step.step_id, flaky_executor)
        assert result == "recovered"
        assert step.state == StepState.COMMITTED
        assert call_count == 2


# =========================================================================
# SagaOrchestrator -- compensate
# =========================================================================


class TestOrchestratorCompensate:

    @pytest.mark.asyncio
    async def test_full_compensation(self):
        orch = SagaOrchestrator()
        saga = orch.create_saga(uuid4())

        # Add and commit two steps.
        s1 = orch.add_step(
            saga.saga_id,
            tool_name="write_file",
            tool_call_id="tc1",
            arguments={},
            checkpoint_hash="hash1",
        )
        s1.transition(StepState.EXECUTING)
        s1.transition(StepState.COMMITTED)

        s2 = orch.add_step(
            saga.saga_id,
            tool_name="patch",
            tool_call_id="tc2",
            arguments={},
            checkpoint_hash="hash2",
        )
        s2.transition(StepState.EXECUTING)
        s2.transition(StepState.COMMITTED)

        compensated_order: list[str] = []

        async def compensator(step: SagaStep) -> str:
            compensated_order.append(step.step_id)
            return "undone"

        failed = await orch.compensate(saga.saga_id, compensator)
        assert failed == []
        assert saga.state == SagaState.COMPLETED
        # Verify reverse order: s2 first, then s1.
        assert compensated_order == [s2.step_id, s1.step_id]
        assert s1.state == StepState.COMPENSATED
        assert s2.state == StepState.COMPENSATED

    @pytest.mark.asyncio
    async def test_partial_compensation_escalates(self):
        orch = SagaOrchestrator()
        saga = orch.create_saga(uuid4())

        s1 = orch.add_step(
            saga.saga_id,
            tool_name="write_file",
            tool_call_id="tc1",
            arguments={},
            checkpoint_hash="hash1",
        )
        s1.transition(StepState.EXECUTING)
        s1.transition(StepState.COMMITTED)

        s2 = orch.add_step(
            saga.saga_id,
            tool_name="patch",
            tool_call_id="tc2",
            arguments={},
            checkpoint_hash="hash2",
        )
        s2.transition(StepState.EXECUTING)
        s2.transition(StepState.COMMITTED)

        async def failing_compensator(step: SagaStep) -> str:
            if step.step_id == s2.step_id:
                raise RuntimeError("undo failed")
            return "ok"

        failed = await orch.compensate(saga.saga_id, failing_compensator)
        assert len(failed) == 1
        assert failed[0].step_id == s2.step_id
        assert saga.state == SagaState.ESCALATED
        assert "1 step(s) failed compensation" in saga.error

    @pytest.mark.asyncio
    async def test_non_compensable_step_fails(self):
        orch = SagaOrchestrator()
        saga = orch.create_saga(uuid4())

        # Step with no checkpoint and no compensation tool.
        s1 = orch.add_step(
            saga.saga_id,
            tool_name="terminal",
            tool_call_id="tc1",
            arguments={},
        )
        s1.transition(StepState.EXECUTING)
        s1.transition(StepState.COMMITTED)

        async def compensator(step: SagaStep) -> str:
            return "ok"

        failed = await orch.compensate(saga.saga_id, compensator)
        assert len(failed) == 1
        assert s1.state == StepState.COMPENSATION_FAILED
        assert saga.state == SagaState.ESCALATED


# =========================================================================
# SagaOrchestrator -- reconstruct_from_events
# =========================================================================


@dataclass
class FakeEvent:
    """Minimal event object for reconstruction tests."""
    id: int
    type: str
    data: dict[str, Any]


class TestOrchestratorReconstruct:

    def test_reconstruct_saga_start(self):
        orch = SagaOrchestrator()
        sid = uuid4()
        events = [
            FakeEvent(1, "saga.start", {
                "saga_id": "saga:abc",
                "session_id": str(sid),
            }),
        ]
        orch.reconstruct_from_events(events)
        saga = orch.get_saga("saga:abc")
        assert saga is not None
        assert saga.session_id == sid
        assert saga.state == SagaState.RUNNING

    def test_reconstruct_step_lifecycle(self):
        orch = SagaOrchestrator()
        sid = uuid4()
        events = [
            FakeEvent(1, "saga.start", {
                "saga_id": "saga:abc",
                "session_id": str(sid),
            }),
            FakeEvent(2, "saga.step_begin", {
                "saga_id": "saga:abc",
                "step_id": "step:1",
                "tool_name": "write_file",
                "tool_call_id": "tc1",
                "checkpoint_hash": "h1",
            }),
            FakeEvent(3, "saga.step_committed", {
                "saga_id": "saga:abc",
                "step_id": "step:1",
                "result": "ok",
            }),
        ]
        orch.reconstruct_from_events(events)
        saga = orch.get_saga("saga:abc")
        assert len(saga.steps) == 1
        assert saga.steps[0].state == StepState.COMMITTED
        assert saga.steps[0].checkpoint_hash == "h1"

    def test_reconstruct_completed_saga(self):
        orch = SagaOrchestrator()
        sid = uuid4()
        events = [
            FakeEvent(1, "saga.start", {
                "saga_id": "saga:abc",
                "session_id": str(sid),
            }),
            FakeEvent(2, "saga.complete", {
                "saga_id": "saga:abc",
                "status": "completed",
            }),
        ]
        orch.reconstruct_from_events(events)
        saga = orch.get_saga("saga:abc")
        assert saga.state == SagaState.COMPLETED
        assert saga not in orch.active_sagas

    def test_reconstruct_ignores_unknown_saga(self):
        """Step events for unknown sagas are silently skipped."""
        orch = SagaOrchestrator()
        events = [
            FakeEvent(1, "saga.step_begin", {
                "saga_id": "saga:unknown",
                "step_id": "step:1",
                "tool_name": "x",
            }),
        ]
        orch.reconstruct_from_events(events)
        assert orch.get_saga("saga:unknown") is None


# =========================================================================
# Compensator dispatch
# =========================================================================


class TestCompensatorDispatch:

    @pytest.mark.asyncio
    async def test_compensate_step_with_checkpoint(self):
        """compensate_step dispatches to compensate_builtin when checkpoint_hash is set."""
        import json
        from surogates.governance.saga.compensator import compensate_step

        step = SagaStep(
            step_id="s1", tool_name="write_file",
            tool_call_id="tc1", arguments={},
            checkpoint_hash="abc123",
        )

        class FakeSandboxPool:
            async def execute(self, session_id: str, tool_name: str, args_str: str) -> str:
                assert tool_name == "_checkpoint"
                args = json.loads(args_str)
                assert args["action"] == "restore"
                assert args["hash"] == "abc123"
                return json.dumps({"success": True, "restored_to": "abc123"})

        result = await compensate_step(
            step,
            sandbox_pool=FakeSandboxPool(),
            session_id="sess-1",
        )
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_compensate_step_with_mcp_tool(self):
        """compensate_step dispatches to compensate_mcp when compensation_tool is set."""
        from surogates.governance.saga.compensator import compensate_step

        step = SagaStep(
            step_id="s1", tool_name="create_ticket",
            tool_call_id="tc1", arguments={},
            compensation_tool="delete_ticket",
            compensation_args={"ticket_id": "T-123"},
        )

        class FakeSandboxPool:
            async def execute(self, session_id: str, tool_name: str, args_str: str) -> str:
                return f"deleted via {tool_name}"

        result = await compensate_step(
            step,
            sandbox_pool=FakeSandboxPool(),
            session_id="sess-1",
        )
        assert "delete_ticket" in result

    @pytest.mark.asyncio
    async def test_compensate_step_non_compensable_raises(self):
        """compensate_step raises for steps with no compensation strategy."""
        from surogates.governance.saga.compensator import compensate_step

        step = SagaStep(
            step_id="s1", tool_name="terminal",
            tool_call_id="tc1", arguments={},
        )

        with pytest.raises(SagaStateError, match="not compensable"):
            await compensate_step(
                step,
                sandbox_pool=None,
                session_id="sess-1",
            )


# =========================================================================
# Integration: 3-step saga with failure and compensation
# =========================================================================


class TestSagaIntegration:

    @pytest.mark.asyncio
    async def test_three_step_saga_failure_compensates_in_reverse(self):
        """Steps 1-2 succeed, step 3 fails, steps 1-2 are compensated in reverse."""
        orch = SagaOrchestrator(retry_delay=0.01)
        saga = orch.create_saga(uuid4())

        # Step 1: succeed.
        s1 = orch.add_step(
            saga.saga_id,
            tool_name="write_file",
            tool_call_id="tc1",
            arguments={"path": "/a"},
            checkpoint_hash="h1",
        )
        await orch.execute_step(saga.saga_id, s1.step_id, _ok_executor("result1"))
        assert s1.state == StepState.COMMITTED

        # Step 2: succeed.
        s2 = orch.add_step(
            saga.saga_id,
            tool_name="patch",
            tool_call_id="tc2",
            arguments={"path": "/b"},
            checkpoint_hash="h2",
        )
        await orch.execute_step(saga.saga_id, s2.step_id, _ok_executor("result2"))
        assert s2.state == StepState.COMMITTED

        # Step 3: fail.
        s3 = orch.add_step(
            saga.saga_id,
            tool_name="terminal",
            tool_call_id="tc3",
            arguments={"command": "deploy"},
        )
        with pytest.raises(RuntimeError):
            await orch.execute_step(saga.saga_id, s3.step_id, _fail_executor("deploy failed"))
        assert s3.state == StepState.FAILED

        # Compensate.
        compensated_order: list[str] = []

        async def compensator(step: SagaStep) -> str:
            compensated_order.append(step.step_id)
            return "undone"

        failed = await orch.compensate(saga.saga_id, compensator)
        # s3 was FAILED (not COMMITTED), so only s1 and s2 are compensated.
        assert failed == []
        assert compensated_order == [s2.step_id, s1.step_id]
        assert saga.state == SagaState.COMPLETED


def _ok_executor(result: str):
    """Return an executor coroutine that succeeds with *result*."""
    async def _exec():
        return result
    return _exec


def _fail_executor(error: str):
    """Return an executor coroutine that raises RuntimeError."""
    async def _exec():
        raise RuntimeError(error)
    return _exec


# =========================================================================
# Additional tests from review
# =========================================================================


class TestCompensationTimeout:

    @pytest.mark.asyncio
    async def test_compensation_timeout_marks_step_as_failed(self):
        """Compensation that exceeds timeout results in COMPENSATION_FAILED."""
        orch = SagaOrchestrator()
        saga = orch.create_saga(uuid4())
        s1 = orch.add_step(
            saga.saga_id,
            tool_name="write_file",
            tool_call_id="tc1",
            arguments={},
            checkpoint_hash="h1",
            timeout_seconds=1,
        )
        s1.transition(StepState.EXECUTING)
        s1.transition(StepState.COMMITTED)

        async def slow_compensator(step: SagaStep) -> str:
            await asyncio.sleep(10)
            return "ok"

        failed = await orch.compensate(saga.saga_id, slow_compensator)
        assert len(failed) == 1
        assert s1.state == StepState.COMPENSATION_FAILED
        assert "timed out" in s1.error
        assert saga.state == SagaState.ESCALATED


class TestCheckpointRestoreFailure:

    @pytest.mark.asyncio
    async def test_checkpoint_restore_failure_raises(self):
        """compensate_builtin raises when restore returns success=False."""
        import json
        from surogates.governance.saga.compensator import compensate_builtin

        step = SagaStep(
            step_id="s1", tool_name="write_file",
            tool_call_id="tc1", arguments={},
            checkpoint_hash="badhash",
        )

        class FailingSandboxPool:
            async def execute(self, session_id: str, tool_name: str, args_str: str) -> str:
                return json.dumps({"success": False, "error": "Checkpoint 'badhash' not found"})

        with pytest.raises(SagaStateError, match="Checkpoint restore failed"):
            await compensate_builtin(step, FailingSandboxPool(), "sess-1")


class TestRetryAllExhausted:

    @pytest.mark.asyncio
    async def test_all_retries_exhausted(self):
        """When all retries fail, the step stays FAILED and the error propagates."""
        orch = SagaOrchestrator(retry_delay=0.01)
        saga = orch.create_saga(uuid4())
        step = orch.add_step(
            saga.saga_id,
            tool_name="terminal",
            tool_call_id="tc1",
            arguments={},
            max_retries=2,
        )

        call_count = 0

        async def always_fails():
            nonlocal call_count
            call_count += 1
            raise RuntimeError(f"fail #{call_count}")

        with pytest.raises(RuntimeError, match="fail #3"):
            await orch.execute_step(saga.saga_id, step.step_id, always_fails)

        assert call_count == 3  # 1 initial + 2 retries
        assert step.state == StepState.FAILED
        assert step.retry_count == 2


class TestReconstructCompensateEvent:

    def test_reconstruct_compensating_saga(self):
        """Reconstruction of a saga that was mid-compensation."""
        orch = SagaOrchestrator()
        sid = uuid4()
        events = [
            FakeEvent(1, "saga.start", {
                "saga_id": "saga:abc",
                "session_id": str(sid),
            }),
            FakeEvent(2, "saga.step_begin", {
                "saga_id": "saga:abc",
                "step_id": "step:1",
                "tool_name": "write_file",
            }),
            FakeEvent(3, "saga.step_committed", {
                "saga_id": "saga:abc",
                "step_id": "step:1",
            }),
            FakeEvent(4, "saga.compensate", {
                "saga_id": "saga:abc",
            }),
        ]
        orch.reconstruct_from_events(events)
        saga = orch.get_saga("saga:abc")
        assert saga.state == SagaState.COMPENSATING
        # active_sagas includes COMPENSATING
        assert saga in orch.active_sagas

    def test_reconstruct_step_failed(self):
        """Reconstruction correctly marks a failed step."""
        orch = SagaOrchestrator()
        sid = uuid4()
        events = [
            FakeEvent(1, "saga.start", {
                "saga_id": "saga:abc",
                "session_id": str(sid),
            }),
            FakeEvent(2, "saga.step_begin", {
                "saga_id": "saga:abc",
                "step_id": "step:1",
                "tool_name": "terminal",
            }),
            FakeEvent(3, "saga.step_failed", {
                "saga_id": "saga:abc",
                "step_id": "step:1",
                "error": "command failed",
            }),
        ]
        orch.reconstruct_from_events(events)
        saga = orch.get_saga("saga:abc")
        assert saga.steps[0].state == StepState.FAILED
        assert saga.steps[0].error == "command failed"
        assert saga.steps[0].completed_at is not None


class TestOrchestratorSettings:

    def test_custom_settings_applied(self):
        """Orchestrator respects custom timeout/retries from settings."""
        orch = SagaOrchestrator(
            default_step_timeout=60,
            default_max_retries=5,
            retry_delay=2.0,
        )
        saga = orch.create_saga(uuid4())
        step = orch.add_step(
            saga.saga_id,
            tool_name="write_file",
            tool_call_id="tc1",
            arguments={},
        )
        assert step.timeout_seconds == 60
        assert step.max_retries == 5

    def test_per_step_override(self):
        """Per-step timeout/retries override orchestrator defaults."""
        orch = SagaOrchestrator(
            default_step_timeout=60,
            default_max_retries=5,
        )
        saga = orch.create_saga(uuid4())
        step = orch.add_step(
            saga.saga_id,
            tool_name="terminal",
            tool_call_id="tc1",
            arguments={},
            timeout_seconds=10,
            max_retries=0,
        )
        assert step.timeout_seconds == 10
        assert step.max_retries == 0
