"""Saga execution, retry, timeout and compensation workflows."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.devices.history import ComputerRefused
from surogates.governance.saga.compensator import UndoOwed, compensate_step, undo_on_computer
from surogates.governance.saga.state_machine import (
    Saga,
    SagaState,
    SagaStep,
    StepState,
)
from surogates.governance.saga.orchestrator import (
    SagaOrchestrator,
    SagaTimeoutError,
    StepRefused,
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

    @pytest.mark.asyncio
    async def test_a_step_refused_for_good_is_not_tried_again(self):
        orch = SagaOrchestrator(retry_delay=0.01)
        saga = orch.create_saga(uuid4())
        step = orch.add_step(saga.saga_id, tool_name="history.pickup", tool_call_id="", arguments={}, max_retries=2)
        tries = 0

        async def refused():
            nonlocal tries
            tries += 1
            raise StepRefused("no such step")

        with pytest.raises(StepRefused):
            await orch.execute_step(saga.saga_id, step.step_id, refused)
        assert (tries, step.state, step.error) == (1, StepState.FAILED, "no such step")


class TestAttempt:
    """A call outside any saga, tried as a step is."""

    @pytest.mark.asyncio
    async def test_a_look_that_fails_or_runs_out_of_time_is_tried_again_as_a_step_is(self):
        orch = SagaOrchestrator(default_step_timeout=1, default_max_retries=2, retry_delay=0.01)
        tries = []

        async def answers_the_third_time():
            tries.append(len(tries))
            if len(tries) == 1:
                raise RuntimeError("the pod did not answer")
            if len(tries) == 2:
                await asyncio.sleep(10)  # past a try's bound
            return "looked"

        assert await orch.attempt(answers_the_third_time) == "looked"
        assert tries == [0, 1, 2]
        assert orch.active_sagas == []  # no saga, and no step, is made for it

    @pytest.mark.asyncio
    async def test_a_look_that_never_answers_raises_its_last_error(self):
        orch = SagaOrchestrator(default_max_retries=1, retry_delay=0.01)
        tries = []

        async def never():
            tries.append(len(tries))
            raise RuntimeError(f"fail #{len(tries)}")

        with pytest.raises(RuntimeError, match="fail #2"):
            await orch.attempt(never)
        assert tries == [0, 1]

    @pytest.mark.asyncio
    async def test_a_look_can_ask_for_a_second_try_where_a_step_has_none(self, monkeypatch):
        orch = SagaOrchestrator(default_max_retries=0, retry_delay=0.25)
        tries, sleep, waits = [], asyncio.sleep, []

        async def answers_the_second_time():
            tries.append(len(tries))
            if len(tries) == 1:
                raise RuntimeError("not there yet")
            return "looked"

        async def counted(seconds, *args):
            waits.append(seconds)
            return await sleep(0)

        monkeypatch.setattr(asyncio, "sleep", counted)
        with pytest.raises(RuntimeError, match="not there yet"):
            await orch.attempt(answers_the_second_time)
        tries.clear()
        waits.clear()
        # At least two tries, the pause of a step's retry between them.
        assert await orch.attempt(answers_the_second_time, least=2) == "looked"
        assert tries == [0, 1] and waits == [0.25]


class TestCompensateStep:

    @pytest.mark.asyncio
    async def test_an_mcp_step_runs_its_undo_tool_and_then_restores_its_snapshot(self):
        """In a thread an MCP step has both: its undo tool, then its copy put back."""
        calls = []

        class Pool:
            async def execute(self, session_id, name, input):
                calls.append(name)
                return '{"success": true}'

        step = SagaStep(
            step_id="s1", tool_name="create_ticket", tool_call_id="tc1", arguments={},
            compensation_tool="delete_ticket", checkpoint_hash="abc123",
        )
        await compensate_step(step, sandbox_pool=Pool(), session_id="s")
        assert calls == ["delete_ticket", "_checkpoint"]


class TestUndoOnComputer:
    """A Stop's undo of a thread's copy on its computer, which a computer away holds up."""

    @staticmethod
    def stopped_turn(timeout: int) -> Saga:
        orch = SagaOrchestrator(default_step_timeout=timeout)
        saga = orch.create_saga(uuid4())
        for n, path in enumerate(("a.md", "b.md")):
            step = orch.add_step(saga.saga_id, tool_name="write_file", tool_call_id=f"tc{n}", arguments={"path": path},
                                 checkpoint_hash=f"{n}" * 40)
            step.transition(StepState.EXECUTING)
            step.transition(StepState.COMMITTED)
        return saga

    @staticmethod
    def away_until(back: asyncio.Event) -> tuple[SimpleNamespace, list]:
        asked: list = []

        async def restore(turn, commit) -> None:
            asked.append(commit)
            await back.wait()

        return SimpleNamespace(restore=restore), asked

    @pytest.mark.asyncio
    async def test_at_the_stop_a_put_back_its_computer_does_not_answer_in_time_leaves_the_rest_owed(self):
        saga = self.stopped_turn(timeout=1)
        copy, asked = self.away_until(asyncio.Event())
        with pytest.raises(UndoOwed) as owed:
            await undo_on_computer(saga, copy=copy, turn=0, ran=lambda step: True)
        # Nothing more is asked once one put-back went unanswered: each step that ran is owed, oldest first.
        assert asked == ["1" * 40]
        assert [(entry["path"], entry["why"]) for entry in owed.value.left] == [("a.md", "owed"), ("b.md", "owed")]

    @pytest.mark.asyncio
    async def test_at_the_stop_a_step_its_undo_can_never_take_back_is_named_why_and_only_the_rest_owed(self):
        orch = SagaOrchestrator(default_step_timeout=1)
        saga = orch.create_saga(uuid4())
        # a.md had no snapshot, before every step that has one; c.md had none either, after b.md's.
        for n, (path, taken) in enumerate((("a.md", None), ("b.md", "1" * 40), ("c.md", None), ("d.md", "3" * 40))):
            step = orch.add_step(saga.saga_id, tool_name="write_file", tool_call_id=f"tc{n}", arguments={"path": path},
                                 checkpoint_hash=taken)
            step.transition(StepState.EXECUTING)
            step.transition(StepState.COMMITTED)

        async def restore(turn, commit) -> None:
            if commit == "3" * 40:
                raise ComputerRefused("history", "The folder's history failed", "failed")
            await asyncio.Event().wait()

        with pytest.raises(UndoOwed) as owed:
            await undo_on_computer(saga, copy=SimpleNamespace(restore=restore), turn=0, ran=lambda step: True)
        # d.md's put-back was refused already, and no snapshot is older than a.md: only b.md's snapshot can take back the rest.
        assert [(entry["path"], entry["why"], entry.get("code")) for entry in owed.value.left] == [
            ("a.md", "no_snapshot", None), ("b.md", "owed", None), ("c.md", "owed", None), ("d.md", "refused", "failed"),
        ]

    @pytest.mark.asyncio
    async def test_at_the_threads_next_turn_each_put_back_waits_for_its_computer_as_long_as_it_takes(self):
        # Its steps' bound is none of its own: past it, it waits on.
        saga = self.stopped_turn(timeout=0)
        back = asyncio.Event()
        copy, asked = self.away_until(back)
        undoing = asyncio.ensure_future(undo_on_computer(saga, copy=copy, turn=0, ran=lambda step: True, bounded=False))
        await asyncio.sleep(0.05)
        assert not undoing.done() and asked == ["1" * 40]
        back.set()
        assert await undoing == [] and saga.state is SagaState.COMPLETED and asked == ["1" * 40, "0" * 40]


class TestSagaRecord:

    def test_a_saga_rebuilt_from_its_record_is_the_saga_as_it_stood(self):
        orch = SagaOrchestrator(default_step_timeout=7, default_max_retries=1)
        saga = orch.create_saga(uuid4(), kind="landing")
        done = orch.add_step(saga.saga_id, tool_name="history.apply", tool_call_id="", arguments={"path": "a.md", "before": None, "after": "b1"})
        done.transition(StepState.EXECUTING)
        done.execute_result = {"path": "a.md", "before": None, "after": "b1", "made": ["notes"]}
        done.transition(StepState.COMMITTED)
        orch.add_step(saga.saga_id, tool_name="history.record", tool_call_id="", arguments={"main": None})
        again = Saga.from_dict(saga.to_dict())
        assert again.to_dict() == saga.to_dict()
        assert (again.kind, again.steps[0].execute_result["made"], again.steps[0].timeout_seconds) == ("landing", ["notes"], 7)
        assert [s.state for s in again.steps] == [StepState.COMMITTED, StepState.PENDING]
        # Taken over, it compensates as the saga it was.
        other = SagaOrchestrator()
        assert other.adopt(again) is other.get_saga(saga.saga_id)

    def test_a_turns_saga_is_of_tools_and_its_start_event_says_so(self):
        from surogates.governance.events import saga_start_event

        orch = SagaOrchestrator()
        saga = orch.create_saga(uuid4())
        assert saga.kind == "tools"
        assert saga_start_event(saga.saga_id, str(saga.session_id), saga.kind)["kind"] == "tools"
        # A log written before sagas had kinds rebuilds tool sagas.
        rebuilt = SagaOrchestrator()
        rebuilt.reconstruct_from_events([SimpleNamespace(type="saga.start", data={"saga_id": "s1", "session_id": str(uuid4())})])
        assert rebuilt.get_saga("s1").kind == "tools"


class TestStopLeft:

    def test_a_stop_left_owed_promises_the_take_back_only_of_the_steps_owed_and_says_what_the_others_do(self):
        from surogates.governance.saga.compensator import (
            STOP_NOT_YET, STOP_NOT_YET_LANDED, STOP_NOT_YET_LEFT_IN_COPY, stop_left,
        )

        owed = {"step_id": "s2", "tool": "write_file", "why": "owed", "path": "b.md"}
        staying = {"step_id": "s1", "tool": "terminal", "why": "no_snapshot"}
        landed = {"step_id": "s0", "tool": "write_file", "why": "landed", "path": "a.md"}
        assert stop_left([owed])["error_title"] == STOP_NOT_YET
        assert stop_left([staying, owed])["error_title"] == STOP_NOT_YET_LEFT_IN_COPY
        assert stop_left([landed, staying, owed])["error_title"] == STOP_NOT_YET_LEFT_IN_COPY
        # Steps already landed are not said to stay in the copy.
        assert stop_left([landed, owed])["error_title"] == STOP_NOT_YET_LANDED
        # Where a step is not owed, the take-back is promised to the steps owed alone, and the others' fate said.
        for title in (STOP_NOT_YET_LEFT_IN_COPY, STOP_NOT_YET_LANDED):
            assert "this turn is taken back" not in title and "not taken back yet are taken back" in title
        assert "What the others changed stays in this thread's copy" in STOP_NOT_YET_LEFT_IN_COPY
        assert "The others had already landed in your folder." in STOP_NOT_YET_LANDED
