"""Metric math and report rendering."""
from wsbench.report import (
    TaskOutcome,
    find_regressions,
    pass_at,
    render,
    summarize,
)


def _outcome(task_id="1", difficulty="medium", passed=3, total=4, **overrides):
    fields = dict(
        task_id=task_id,
        persona="Researcher",
        difficulty=difficulty,
        total_rubrics=total,
        passed_rubrics=passed,
        by_rubric_type={"Outcome Evaluation": [passed, total]},
    )
    fields.update(overrides)
    return TaskOutcome(**fields)


def test_pass_at_thresholds_are_inclusive():
    o = _outcome(passed=3, total=5)  # 60%
    assert pass_at(o, 50) is True
    assert pass_at(o, 60) is True
    assert pass_at(o, 80) is False


def test_zero_rubrics_never_passes():
    o = _outcome(passed=0, total=0)
    assert o.pass_fraction == 0.0
    assert pass_at(o, 50) is False


def test_summarize_micro_average_and_difficulty():
    outcomes = [
        _outcome("1", "easy", passed=4, total=4),
        _outcome("2", "hard", passed=1, total=6),
    ]
    s = summarize(outcomes)
    assert s["rubric_pass_rate"] == 50.0  # 5/10 micro, not mean of 100%/16.7%
    assert s["by_difficulty"]["easy"]["accuracy"] == 100.0
    assert s["by_difficulty"]["hard"]["accuracy"] == round(100 / 6, 1)
    assert s["pass_at"][50] == 50.0
    assert s["by_rubric_type"]["Outcome Evaluation"] == 50.0


def test_find_regressions_flags_pass60_flips_only():
    prev = [_outcome("1", passed=4, total=4), _outcome("2", passed=1, total=4)]
    cur = [_outcome("1", passed=1, total=4), _outcome("2", passed=0, total=4)]
    # Task 2 was already failing; only task 1 regressed.
    assert find_regressions(prev, cur) == ["1"]


def test_render_lists_failed_tasks_with_reason():
    outcomes = [
        _outcome("1", passed=4, total=4),
        _outcome("2", passed=0, total=4,
                 missing_outputs=["report.md"], terminal_status="completed"),
        _outcome("3", passed=0, total=4, terminal_status="failed",
                 error="HarnessError: boom"),
    ]
    text = render(outcomes, run_id="dev-001")
    assert "Rubric Pass Rate" in text
    assert "`2`" in text and "missing outputs: report.md" in text
    assert "`3`" in text and "HarnessError: boom" in text
    assert "| `1` |" not in text  # passing task is not in the failed table


def test_render_regressions_come_first():
    prev = [_outcome("1", passed=4, total=4)]
    cur = [_outcome("1", passed=0, total=4)]
    text = render(cur, previous=prev, run_id="dev-002")
    assert text.index("## Regressions") < text.index("## Score")
    assert "- `1`" in text
