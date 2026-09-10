"""Saga execution, retry, timeout and compensation workflows."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from surogates.governance.saga.state_machine import (
    SagaState,
    SagaStep,
    StepState,
)
from surogates.governance.saga.orchestrator import (
    SagaOrchestrator,
    SagaTimeoutError,
)


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
