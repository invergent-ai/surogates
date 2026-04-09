"""Tests for surogates.harness.cost_tracker -- per-session cost accumulation."""

from __future__ import annotations

from surogates.harness.cost_tracker import SessionCostTracker


class TestSessionCostTracker:
    def test_initial_state(self) -> None:
        tracker = SessionCostTracker()
        assert tracker.total_input_tokens == 0
        assert tracker.total_output_tokens == 0
        assert tracker.total_cache_read_tokens == 0
        assert tracker.total_reasoning_tokens == 0
        assert tracker.total_cost_usd == 0.0
        assert tracker.call_count == 0

    def test_record_single_call(self) -> None:
        tracker = SessionCostTracker()
        tracker.record_call(
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.001,
        )
        assert tracker.total_input_tokens == 100
        assert tracker.total_output_tokens == 50
        assert tracker.total_cost_usd == 0.001
        assert tracker.call_count == 1

    def test_record_multiple_calls_accumulates(self) -> None:
        tracker = SessionCostTracker()
        tracker.record_call(input_tokens=100, output_tokens=50, cost_usd=0.001)
        tracker.record_call(input_tokens=200, output_tokens=100, cost_usd=0.003)
        tracker.record_call(input_tokens=150, output_tokens=75, cost_usd=0.002)
        assert tracker.total_input_tokens == 450
        assert tracker.total_output_tokens == 225
        assert tracker.call_count == 3
        assert abs(tracker.total_cost_usd - 0.006) < 1e-9

    def test_record_call_with_cache_tokens(self) -> None:
        tracker = SessionCostTracker()
        tracker.record_call(
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.001,
            cache_read_tokens=80,
        )
        assert tracker.total_cache_read_tokens == 80

    def test_record_call_with_reasoning_tokens(self) -> None:
        tracker = SessionCostTracker()
        tracker.record_call(
            input_tokens=100,
            output_tokens=500,
            cost_usd=0.05,
            reasoning_tokens=300,
        )
        assert tracker.total_reasoning_tokens == 300

    def test_record_call_with_all_optional_fields(self) -> None:
        tracker = SessionCostTracker()
        tracker.record_call(
            input_tokens=1000,
            output_tokens=500,
            cost_usd=0.01,
            cache_read_tokens=800,
            reasoning_tokens=200,
        )
        assert tracker.total_cache_read_tokens == 800
        assert tracker.total_reasoning_tokens == 200

    def test_summary_returns_correct_dict(self) -> None:
        tracker = SessionCostTracker()
        tracker.record_call(input_tokens=100, output_tokens=50, cost_usd=0.001)
        tracker.record_call(
            input_tokens=200, output_tokens=100, cost_usd=0.003,
            cache_read_tokens=150, reasoning_tokens=50,
        )
        summary = tracker.summary()
        assert summary == {
            "total_input_tokens": 300,
            "total_output_tokens": 150,
            "total_cache_read_tokens": 150,
            "total_reasoning_tokens": 50,
            "total_cost_usd": 0.004,
            "call_count": 2,
        }

    def test_summary_rounds_cost(self) -> None:
        tracker = SessionCostTracker()
        # Accumulate floating point values that would need rounding.
        for _ in range(7):
            tracker.record_call(input_tokens=10, output_tokens=5, cost_usd=0.0000001)
        summary = tracker.summary()
        assert summary["total_cost_usd"] == round(0.0000001 * 7, 6)

    def test_summary_empty_tracker(self) -> None:
        tracker = SessionCostTracker()
        summary = tracker.summary()
        assert summary["call_count"] == 0
        assert summary["total_cost_usd"] == 0.0

    def test_zero_cost_call(self) -> None:
        tracker = SessionCostTracker()
        tracker.record_call(input_tokens=100, output_tokens=50, cost_usd=0.0)
        assert tracker.call_count == 1
        assert tracker.total_cost_usd == 0.0
