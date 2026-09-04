from claweval_bench.report import passed, render


def outcome(**kw):
    base = {
        "task_id": "T001_x", "category": "ops", "terminal_status": "completed",
        "wall_clock_s": 10.0, "error": None, "grader_error": None,
        "scores": {"completion": 0.9, "safety": 1.0, "robustness": 0.8,
                   "communication": 0.7},
    }
    base.update(kw)
    return base


def test_pass_needs_safety_gate_and_completion():
    assert passed(outcome())
    assert not passed(outcome(scores={"completion": 0.9, "safety": 0.0}))
    assert not passed(outcome(scores={"completion": 0.5, "safety": 1.0}))
    assert not passed(outcome(scores=None))


def test_render_lists_failed_tasks_with_reasons():
    outcomes = [
        outcome(),
        outcome(task_id="T002_y", scores={"completion": 0.2, "safety": 1.0}),
        outcome(task_id="T003_z", terminal_status="error",
                error="HarnessError: HTTP 503", scores=None),
        outcome(task_id="T004_s", scores={"completion": 0.9, "safety": 0.0}),
    ]
    text = render(outcomes, run_id="general-001", skipped={"M01": "needs media"})
    assert "**1/4**" in text
    assert "| `T002_y` |" in text and "completion 0.20" in text
    assert "| `T003_z` |" in text and "rollout error" in text
    assert "| `T004_s` |" in text and "safety gate failed" in text
    assert "`T001_x`" not in text.split("## Failed tasks")[1]
    assert "needs media" in text


def test_render_counts_grader_crashes_as_failures():
    text = render([outcome(scores=None, grader_error="KeyError: 'x'")],
                  run_id="r")
    assert "**0/1**" in text
    assert "grader crashed" in text
