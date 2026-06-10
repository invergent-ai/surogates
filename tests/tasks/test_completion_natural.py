"""A worker session that ends cleanly completes its task without worker_complete.

Regression for: task-backed worker built the app + tests passed, ended its
session naturally (no worker_complete tool call), and the reconciler wrongly
classified it CRASHED -> retried/rebuilt -> failed.
"""

from __future__ import annotations

from surogates.session.events import EventType
from surogates.tasks.completion import (
    TaskAttemptOutcome,
    classify_attempt_outcome,
    extract_result_from_completion_event,
)


class _Ev:
    def __init__(self, etype, data, eid=0):
        self.type = etype
        self.data = data
        self.id = eid


def _llm_response(text, eid=0):
    return _Ev(EventType.LLM_RESPONSE.value,
               {"message": {"role": "assistant", "content": text}}, eid)


def test_worker_complete_still_wins():
    events = [
        _llm_response("working", 1),
        _Ev(EventType.WORKER_COMPLETE.value, {"result": "explicit handoff"}, 2),
    ]
    outcome, ev = classify_attempt_outcome(events, session_status="completed")
    assert outcome is TaskAttemptOutcome.COMPLETED
    assert extract_result_from_completion_event(ev) == "explicit handoff"


def test_task_blocked_still_wins():
    events = [_Ev(EventType.TASK_BLOCKED.value, {"reason": "review-required: x"}, 1)]
    outcome, _ = classify_attempt_outcome(events, session_status="completed")
    assert outcome is TaskAttemptOutcome.BLOCKED


def test_natural_completion_is_completed_with_extracted_result():
    # No worker_complete call, but the session ended 'completed'.
    events = [
        _llm_response("building...", 1),
        _llm_response("All 4 tests pass. URL shortener built and verified.", 2),
        _Ev(EventType.SESSION_COMPLETE.value, {"reason": "completed"}, 3),
    ]
    outcome, ev = classify_attempt_outcome(events, session_status="completed")
    assert outcome is TaskAttemptOutcome.COMPLETED
    result = extract_result_from_completion_event(ev)
    assert result is not None
    assert "tests pass" in result


def test_failed_session_is_crashed():
    events = [_llm_response("I couldn't finish", 1)]
    outcome, _ = classify_attempt_outcome(events, session_status="failed")
    assert outcome is TaskAttemptOutcome.CRASHED


def test_no_status_defaults_to_crashed_without_outcome_event():
    # Backward-compat: callers that don't pass session_status keep old behavior.
    events = [_llm_response("partial work", 1)]
    outcome, _ = classify_attempt_outcome(events)
    assert outcome is TaskAttemptOutcome.CRASHED
