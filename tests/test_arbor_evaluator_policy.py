"""Unit tests for the research-kind mission judge policy."""
from __future__ import annotations

import pytest

from surogates.arbor.evaluator_policy import (
    adjust_research_verdict,
    research_prompt_block,
    research_should_skip,
)


@pytest.mark.asyncio
async def test_skip_while_experiments_in_flight():
    class _Store:
        async def in_flight_count(self, run_id):
            return 2

    assert await research_should_skip(_Store(), "run") is True


@pytest.mark.asyncio
async def test_no_skip_when_idle():
    class _Store:
        async def in_flight_count(self, run_id):
            return 0

    assert await research_should_skip(_Store(), "run") is False


def test_satisfied_requires_machine_written_score_and_report():
    meta = {"test_baseline_score": 0.5, "metric_direction": "maximize"}
    verdict = {"result": "satisfied", "explanation": "looks done", "feedback": ""}

    # No test_trunk_score yet -> demoted.
    out = adjust_research_verdict(verdict, meta=meta, report_task_done=True)
    assert out["result"] == "needs_revision"

    # Score present but report task not done -> demoted.
    meta["test_trunk_score"] = 0.6
    out = adjust_research_verdict(verdict, meta=meta, report_task_done=False)
    assert out["result"] == "needs_revision"

    # Score improving AND report done -> satisfied stands.
    out = adjust_research_verdict(verdict, meta=meta, report_task_done=True)
    assert out["result"] == "satisfied"


def test_satisfied_via_budget_exhausted_no_improvement_close():
    # Budget spent, no improvement, but report done and judge says satisfied
    # (an explicit no-improvement close) -> allowed.
    meta = {"test_baseline_score": 0.5, "metric_direction": "maximize"}
    verdict = {"result": "satisfied", "explanation": "", "feedback": ""}
    out = adjust_research_verdict(
        verdict, meta=meta, report_task_done=True, budget_exhausted=True,
    )
    assert out["result"] == "satisfied"


def test_failed_and_blocked_demote_without_corroboration():
    meta: dict = {}
    for noisy in ("failed", "blocked"):
        out = adjust_research_verdict(
            {"result": noisy, "explanation": "", "feedback": ""},
            meta=meta, report_task_done=False,
        )
        assert out["result"] == "needs_revision"


def test_failed_stands_when_corroborated():
    out = adjust_research_verdict(
        {"result": "failed", "explanation": "", "feedback": ""},
        meta={}, report_task_done=False, budget_exhausted=True,
    )
    assert out["result"] == "failed"


def test_needs_revision_passes_through():
    out = adjust_research_verdict(
        {"result": "needs_revision", "explanation": "", "feedback": "expand X"},
        meta={}, report_task_done=False,
    )
    assert out["result"] == "needs_revision"


def test_prompt_block_carries_leaderboard_and_guidance():
    block = research_prompt_block(
        constraints_block="### TREE SHAPE\n- 1 [done] score=0.4 idea",
        cycles_spent=3, max_cycles=10,
    )
    assert "3/10" in block
    assert "TREE SHAPE" in block
    assert "test_trunk_score" in block
