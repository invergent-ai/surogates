"""Saga orchestrator -- sequential step execution with reverse-order compensation.

Adapted from Microsoft Agent Governance Toolkit
(agent-hypervisor/saga/orchestrator.py).  The core algorithm is identical:
forward execution with timeout + retry, reverse-order compensation on
failure, ESCALATED state when compensation itself fails.

Adds ``reconstruct_from_events`` for crash-recovery in Surogates' stateless
harness model (AGT is single-process and doesn't need it).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from surogates.governance.saga.state_machine import (
    Saga,
    SagaState,
    SagaStateError,
    SagaStep,
    StepState,
)

if TYPE_CHECKING:
    from surogates.session.models import Event

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SAGA_DEFAULT_STEP_TIMEOUT_SECONDS: int = 300
SAGA_DEFAULT_RETRY_DELAY_SECONDS: float = 1.0
SAGA_DEFAULT_MAX_RETRIES: int = 2


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SagaTimeoutError(Exception):
    """Raised when a saga step exceeds its timeout."""


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class SagaOrchestrator:
    """Orchestrates multi-step tool transactions with saga semantics.

    Forward execution records each step.  On failure the orchestrator
    iterates committed steps in reverse order, calling the supplied
    compensator for each.  If any compensation fails the saga enters
    the ESCALATED state.

    Parameters
    ----------
    default_step_timeout:
        Default timeout (seconds) for each saga step.
    default_max_retries:
        Default number of retries for each saga step.
    retry_delay:
        Base delay (seconds) between retries (multiplied by attempt number).
    """

    def __init__(
        self,
        *,
        default_step_timeout: int = SAGA_DEFAULT_STEP_TIMEOUT_SECONDS,
        default_max_retries: int = SAGA_DEFAULT_MAX_RETRIES,
        retry_delay: float = SAGA_DEFAULT_RETRY_DELAY_SECONDS,
    ) -> None:
        self._sagas: dict[str, Saga] = {}
        self._default_step_timeout = default_step_timeout
        self._default_max_retries = default_max_retries
        self._retry_delay = retry_delay

    # ------------------------------------------------------------------
    # Saga lifecycle
    # ------------------------------------------------------------------

    def create_saga(self, session_id: UUID) -> Saga:
        """Create a new saga for *session_id*."""
        saga = Saga(
            saga_id=f"saga:{uuid.uuid4()}",
            session_id=session_id,
        )
        self._sagas[saga.saga_id] = saga
        return saga

    def add_step(
        self,
        saga_id: str,
        *,
        tool_name: str,
        tool_call_id: str,
        arguments: dict[str, Any],
        compensation_tool: str | None = None,
        compensation_args: dict[str, Any] | None = None,
        checkpoint_hash: str | None = None,
        timeout_seconds: int | None = None,
        max_retries: int | None = None,
    ) -> SagaStep:
        """Add a step to saga *saga_id*."""
        saga = self._get_saga(saga_id)
        step = SagaStep(
            step_id=f"step:{uuid.uuid4()}",
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            arguments=arguments,
            compensation_tool=compensation_tool,
            compensation_args=compensation_args,
            checkpoint_hash=checkpoint_hash,
            timeout_seconds=timeout_seconds if timeout_seconds is not None else self._default_step_timeout,
            max_retries=max_retries if max_retries is not None else self._default_max_retries,
        )
        saga.steps.append(step)
        return step

    # ------------------------------------------------------------------
    # Forward execution
    # ------------------------------------------------------------------

    async def execute_step(
        self,
        saga_id: str,
        step_id: str,
        executor: Callable[..., Any],
    ) -> Any:
        """Execute a single saga step with timeout and retry support.

        Parameters
        ----------
        saga_id:
            Saga identifier.
        step_id:
            Step identifier.
        executor:
            Async callable that performs the tool call and returns its
            result.

        Returns
        -------
        Any
            The result returned by *executor*.

        Raises
        ------
        SagaStateError
            If the step is not in PENDING state.
        SagaTimeoutError
            If the step exceeds its timeout on every attempt.
        """
        saga = self._get_saga(saga_id)
        step = self._get_step(saga, step_id)

        last_error: Exception | None = None
        attempts = 1 + step.max_retries

        for attempt in range(attempts):
            step.retry_count = attempt
            step.transition(StepState.EXECUTING)
            try:
                result = await asyncio.wait_for(
                    executor(),
                    timeout=step.timeout_seconds,
                )
                step.execute_result = result
                step.transition(StepState.COMMITTED)
                return result
            except TimeoutError:
                last_error = SagaTimeoutError(
                    f"Step {step_id} timed out after {step.timeout_seconds}s "
                    f"(attempt {attempt + 1}/{attempts})"
                )
                step.error = str(last_error)
                step.transition(StepState.FAILED)
                if attempt < attempts - 1:
                    # Reset to PENDING for retry.
                    step.state = StepState.PENDING
                    step.error = None
                    await asyncio.sleep(
                        self._retry_delay * (attempt + 1)
                    )
            except Exception as exc:
                last_error = exc
                step.error = str(exc)
                step.transition(StepState.FAILED)
                if attempt < attempts - 1:
                    step.state = StepState.PENDING
                    step.error = None
                    await asyncio.sleep(
                        self._retry_delay * (attempt + 1)
                    )

        # All retries exhausted.
        if last_error:
            raise last_error
        raise SagaStateError("Step execution failed with no error captured")

    # ------------------------------------------------------------------
    # Compensation (rollback)
    # ------------------------------------------------------------------

    async def compensate(
        self,
        saga_id: str,
        compensator: Callable[[SagaStep], Any],
    ) -> list[SagaStep]:
        """Run compensation for all committed steps in reverse order.

        Parameters
        ----------
        saga_id:
            Saga identifier.
        compensator:
            Async callable that takes a :class:`SagaStep` and executes
            its compensation action (checkpoint restore or undo tool
            call).

        Returns
        -------
        list[SagaStep]
            Steps that failed compensation (empty means full success).
        """
        saga = self._get_saga(saga_id)
        saga.transition(SagaState.COMPENSATING)

        failed_compensations: list[SagaStep] = []

        for step in saga.committed_steps_reversed:
            if not step.is_compensable:
                step.state = StepState.COMPENSATION_FAILED
                step.error = "No compensation strategy available"
                failed_compensations.append(step)
                continue

            step.transition(StepState.COMPENSATING)
            try:
                result = await asyncio.wait_for(
                    compensator(step),
                    timeout=step.timeout_seconds,
                )
                step.compensation_result = result
                step.transition(StepState.COMPENSATED)
            except TimeoutError:
                step.error = f"Compensation timed out after {step.timeout_seconds}s"
                step.transition(StepState.COMPENSATION_FAILED)
                failed_compensations.append(step)
            except Exception as exc:
                step.error = f"Compensation failed: {exc}"
                step.transition(StepState.COMPENSATION_FAILED)
                failed_compensations.append(step)

        if failed_compensations:
            saga.transition(SagaState.ESCALATED)
            saga.error = (
                f"{len(failed_compensations)} step(s) failed compensation"
            )
        else:
            saga.transition(SagaState.COMPLETED)

        return failed_compensations

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_saga(self, saga_id: str) -> Saga | None:
        """Get a saga by ID, or ``None`` if not found."""
        return self._sagas.get(saga_id)

    @property
    def current_saga(self) -> Saga | None:
        """The first active (non-terminal) saga, or ``None``.

        Most sessions have at most one active saga, so this is the
        preferred accessor on the hot path (avoids building a list).
        """
        for s in self._sagas.values():
            if s.state in (SagaState.RUNNING, SagaState.COMPENSATING):
                return s
        return None

    @property
    def active_sagas(self) -> list[Saga]:
        """All non-terminal sagas (RUNNING or COMPENSATING)."""
        return [
            s for s in self._sagas.values()
            if s.state in (SagaState.RUNNING, SagaState.COMPENSATING)
        ]

    # ------------------------------------------------------------------
    # Crash recovery
    # ------------------------------------------------------------------

    def reconstruct_from_events(self, events: list[Event]) -> None:
        """Rebuild in-memory saga state from the session event log.

        Called during ``AgentHarness.wake()`` to recover any saga that
        was in progress when the previous harness crashed or timed out.

        Only processes events whose ``type`` starts with ``"saga."``.
        Events are expected in chronological order (ascending ``id``).
        """
        for event in events:
            etype = event.type
            data = event.data or {}

            if etype == "saga.start":
                saga = Saga(
                    saga_id=data["saga_id"],
                    session_id=UUID(data["session_id"]),
                )
                self._sagas[saga.saga_id] = saga

            elif etype == "saga.step_begin":
                saga = self._sagas.get(data.get("saga_id", ""))
                if saga is None:
                    continue
                step = SagaStep(
                    step_id=data["step_id"],
                    tool_name=data["tool_name"],
                    tool_call_id=data.get("tool_call_id", ""),
                    arguments=data.get("arguments", {}),
                    compensation_tool=data.get("compensation_tool"),
                    compensation_args=data.get("compensation_args"),
                    checkpoint_hash=data.get("checkpoint_hash"),
                    state=StepState.EXECUTING,
                )
                saga.steps.append(step)

            elif etype == "saga.step_committed":
                step = self._find_step(data.get("saga_id", ""), data.get("step_id", ""))
                if step is not None:
                    step.state = StepState.COMMITTED
                    step.completed_at = datetime.now(UTC)
                    step.execute_result = data.get("result")

            elif etype == "saga.step_failed":
                step = self._find_step(data.get("saga_id", ""), data.get("step_id", ""))
                if step is not None:
                    step.state = StepState.FAILED
                    step.completed_at = datetime.now(UTC)
                    step.error = data.get("error")

            elif etype == "saga.compensate":
                saga = self._sagas.get(data.get("saga_id", ""))
                if saga is not None:
                    saga.state = SagaState.COMPENSATING

            elif etype == "saga.complete":
                saga = self._sagas.get(data.get("saga_id", ""))
                if saga is not None:
                    status = data.get("status", "completed")
                    # Bypass the state machine during replay — the
                    # transitions already happened in the original run.
                    saga.state = SagaState(status)
                    if saga.state in (SagaState.COMPLETED, SagaState.FAILED, SagaState.ESCALATED):
                        saga.completed_at = datetime.now(UTC)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_saga(self, saga_id: str) -> Saga:
        saga = self._sagas.get(saga_id)
        if not saga:
            raise SagaStateError(f"Saga {saga_id} not found")
        return saga

    def _get_step(self, saga: Saga, step_id: str) -> SagaStep:
        for step in saga.steps:
            if step.step_id == step_id:
                return step
        raise SagaStateError(f"Step {step_id} not found in saga {saga.saga_id}")

    def _find_step(self, saga_id: str, step_id: str) -> SagaStep | None:
        """Lenient lookup -- returns ``None`` instead of raising."""
        saga = self._sagas.get(saga_id)
        if saga is None:
            return None
        for step in saga.steps:
            if step.step_id == step_id:
                return step
        return None
