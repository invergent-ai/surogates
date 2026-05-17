"""Unit tests for the evaluator's pure-function pieces."""
from __future__ import annotations


def test_response_contains_completion_marker_positive():
    """A response with [[mission-complete]] on its own line triggers."""
    from surogates.missions.evaluator import response_claims_completion

    body = "I've trained the model.\n\n[[mission-complete]]\n\nLogs attached."
    assert response_claims_completion(body) is True


def test_response_contains_completion_marker_inside_prose_no_trigger():
    """[[mission-complete]] inside running prose does NOT trigger
    (must be its own line)."""
    from surogates.missions.evaluator import response_claims_completion

    body = "I'll mark this with [[mission-complete]] when I'm done, but not yet."
    assert response_claims_completion(body) is False


def test_response_contains_completion_marker_negative():
    """A regular response does not trigger."""
    from surogates.missions.evaluator import response_claims_completion

    assert response_claims_completion("just regular work output") is False


def test_response_contains_completion_marker_empty():
    from surogates.missions.evaluator import response_claims_completion

    assert response_claims_completion("") is False
    assert response_claims_completion(None) is False
