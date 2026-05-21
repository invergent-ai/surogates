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
    # Emitted by the streaming watchdog when the upstream has been
    # silent past STREAM_HEARTBEAT_INTERVAL but is still inside the
    # stale-timeout window. Lets the UI distinguish "model is silently
    # reasoning" from "stream is dead".
    LLM_HEARTBEAT = "llm.heartbeat"

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
    # Auto-title lands on ``sessions.title`` outside the chat-turn event flow.
    # Emitting this lets SSE subscribers patch the title in place instead of
    # waiting for an unrelated refresh.
    SESSION_TITLE_UPDATED = "session.title_updated"

    # Outcome-oriented goal loop
    USER_DEFINE_OUTCOME = "user.define_outcome"
    OUTCOME_DEFINED = "outcome.defined"
    OUTCOME_PAUSED = "outcome.paused"
    OUTCOME_CLEARED = "outcome.cleared"
    OUTCOME_EVALUATION_START = "span.outcome_evaluation_start"
    OUTCOME_EVALUATION_ONGOING = "span.outcome_evaluation_ongoing"
    OUTCOME_EVALUATION_END = "span.outcome_evaluation_end"
    OUTCOME_CONTINUATION = "outcome.continuation"

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
    # Emitted by the orchestrator's orphan sweeper when a session has
    # been abandoned by a dead worker (no lease, no recent events).
    # Distinct from HARNESS_CRASH because no exception was actually
    # raised — the worker was hard-killed (SIGKILL, OOM, pod eviction,
    # debugger stop) — so this event documents the gap in the log and
    # triggers the dispatcher's retry path when the session re-wakes.
    HARNESS_RECOVERED = "harness.recovered"

    # Sub-agent delegation (delegate_task tool)
    DELEGATION_START = "delegation.start"
    DELEGATION_COMPLETE = "delegation.complete"
    DELEGATION_FAILED = "delegation.failed"
    DELEGATION_STALE = "delegation.stale"

    # Expert delegation
    EXPERT_DELEGATION = "expert.delegation"
    EXPERT_RESULT = "expert.result"
    EXPERT_FAILURE = "expert.failure"
    # User feedback on an expert.result — rates the expert's output.
    # Emitted by POST /v1/sessions/{id}/events/{event_id}/feedback.
    EXPERT_ENDORSE = "expert.endorse"     # thumbs-up
    EXPERT_OVERRIDE = "expert.override"   # thumbs-down

    # Hidden harness advisor. The executor never sees an advisor tool; these
    # events audit the auxiliary guidance pass and provider usage.
    ADVISOR_REQUEST = "advisor.request"
    ADVISOR_RESULT = "advisor.result"
    ADVISOR_FAILURE = "advisor.failure"

    # Worker coordination (coordinator mode)
    WORKER_SPAWNED = "worker.spawned"
    WORKER_COMPLETE = "worker.complete"
    WORKER_FAILED = "worker.failed"

    # Subagent task layer (spawn_task tool / tasks_tick dispatcher).
    # Emitted to the parent (spawning) session so the coordinator agent
    # observes task state transitions on its next wake. ``WORKER_COMPLETE``
    # is reused for successful task completion; the payload carries the
    # ``task_id`` so the parent can correlate.
    TASK_BLOCKED = "task.blocked"
    TASK_FAILED = "task.failed"

    # Mission layer (orchestrated goals).
    # Emitted on the coordinator chat session. The dashboard polls these
    # to render mission state; see
    # docs/superpowers/specs/2026-05-16-mission-orchestrated-goals-design.md.
    MISSION_DEFINED = "mission.defined"
    MISSION_EVALUATION_START = "mission.evaluation.start"
    MISSION_EVALUATION_END = "mission.evaluation.end"
    MISSION_CONTINUATION = "mission.continuation"
    MISSION_PAUSED = "mission.paused"
    MISSION_RESUMED = "mission.resumed"
    MISSION_CANCELLED = "mission.cancelled"

    # Governance
    POLICY_ALLOWED = "policy.allowed"
    POLICY_DENIED = "policy.denied"

    # General user feedback on an llm.response (not expert.result — that is
    # rated via EXPERT_ENDORSE / EXPERT_OVERRIDE).  Emitted by the feedback
    # endpoint and consumed by training-data selection to filter trajectories
    # the user explicitly rated.
    USER_FEEDBACK = "user.feedback"

    # Artifacts — LLM-built inline content (charts, tables, markdown) stored
    # in the session workspace and rendered in the chat thread.  Events carry
    # metadata only; the payload is fetched on-demand via the artifacts API.
    ARTIFACT_CREATED = "artifact.created"
    ARTIFACT_UPDATED = "artifact.updated"

    # Clarify — user's response to a `clarify` tool call.  Emitted by the
    # clarify response endpoint when the user submits answers through the
    # web widget.  The worker's clarify handler polls the event log for a
    # matching ``tool_call_id`` and returns the responses to the LLM.
    # Session replay uses this to re-lock the widget after a page reload.
    CLARIFY_RESPONSE = "clarify.response"

    # Saga orchestration
    SAGA_START = "saga.start"
    SAGA_STEP_BEGIN = "saga.step_begin"
    SAGA_STEP_COMMITTED = "saga.step_committed"
    SAGA_STEP_FAILED = "saga.step_failed"
    SAGA_COMPENSATE = "saga.compensate"
    SAGA_COMPLETE = "saga.complete"

    # Agent browser lifecycle
    BROWSER_PROVISIONED = "browser.provisioned"
    BROWSER_DESTROYED = "browser.destroyed"
    BROWSER_CONTROL_GRANTED = "browser.control_granted"
    BROWSER_CONTROL_RETURNED = "browser.control_returned"

    # Agent inbox
    INBOX_INPUT_REQUIRED = "inbox.input_required"
    INBOX_ACTION_REQUIRED = "inbox.action_required"
    INBOX_TASK_COMPLETE = "inbox.task_complete"
    INBOX_GOVERNANCE_GATE = "inbox.governance_gate"
    INBOX_PROGRESS_CHECKIN = "inbox.progress_checkin"
