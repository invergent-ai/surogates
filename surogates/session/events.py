"""Event type enumeration for the append-only session event log."""

from __future__ import annotations

from enum import unique
from enum import Enum


@unique
class EventType(str, Enum):
    """Every event in the system's append-only log has exactly one of these types.

    The string values use a ``<domain>.<verb>`` convention so they read
    naturally in JSON payloads and database rows.
    """

    # User interaction
    USER_MESSAGE = "user.message"

    # LLM interaction
    LLM_REQUEST = "llm.request"
    LLM_RESPONSE = "llm.response"
    LLM_THINKING = "llm.thinking"
    LLM_DELTA = "llm.delta"

    # Tool execution
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"

    # Sandbox lifecycle
    SANDBOX_PROVISION = "sandbox.provision"
    SANDBOX_EXECUTE = "sandbox.execute"
    SANDBOX_RESULT = "sandbox.result"
    SANDBOX_DESTROY = "sandbox.destroy"

    # Session lifecycle
    SESSION_START = "session.start"
    SESSION_PAUSE = "session.pause"
    SESSION_RESUME = "session.resume"
    SESSION_COMPLETE = "session.complete"
    SESSION_FAIL = "session.fail"
    SESSION_RESET = "session.reset"

    # Context management
    CONTEXT_COMPACT = "context.compact"
    MEMORY_UPDATE = "memory.update"

    # Skill invocation via slash-command (e.g. user typed "/arxiv ...").
    # Recorded so the audit log shows the user's intent and which skill the
    # harness eagerly inlined before the LLM saw the message.
    SKILL_INVOKED = "skill.invoked"

    # Harness lifecycle
    HARNESS_WAKE = "harness.wake"
    HARNESS_CRASH = "harness.crash"

    # Expert delegation
    EXPERT_DELEGATION = "expert.delegation"
    EXPERT_RESULT = "expert.result"
    EXPERT_FAILURE = "expert.failure"
    # User feedback on an expert.result — rates the expert's output.
    # Emitted by POST /v1/sessions/{id}/events/{event_id}/feedback.
    EXPERT_ENDORSE = "expert.endorse"     # thumbs-up
    EXPERT_OVERRIDE = "expert.override"   # thumbs-down

    # Worker coordination (coordinator mode)
    WORKER_SPAWNED = "worker.spawned"
    WORKER_COMPLETE = "worker.complete"
    WORKER_FAILED = "worker.failed"

    # Governance
    POLICY_ALLOWED = "policy.allowed"
    POLICY_DENIED = "policy.denied"

    # General user feedback on an llm.response (not expert.result — that is
    # rated via EXPERT_ENDORSE / EXPERT_OVERRIDE).  Emitted by the feedback
    # endpoint and consumed by training-data selection to filter trajectories
    # the user explicitly rated.
    USER_FEEDBACK = "user.feedback"

    # Saga orchestration
    SAGA_START = "saga.start"
    SAGA_STEP_BEGIN = "saga.step_begin"
    SAGA_STEP_COMMITTED = "saga.step_committed"
    SAGA_STEP_FAILED = "saga.step_failed"
    SAGA_COMPENSATE = "saga.compensate"
    SAGA_COMPLETE = "saga.complete"
