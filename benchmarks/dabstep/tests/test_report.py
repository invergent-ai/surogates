"""Accuracy math, ungradable handling, regressions, rendering."""
from dabbench.report import TaskOutcome, find_regressions, render, summarize


def _outcome(task_id="1", level="easy", correct=True, **overrides):
    fields = dict(
        task_id=task_id,
        level=level,
        answer="42",
        correct=correct,
        key_source="task_scores",
        terminal_status="completed",
    )
    fields.update(overrides)
    return TaskOutcome(**fields)


def test_summarize_excludes_ungradable_from_accuracy():
    outcomes = [
        _outcome("1", "easy", True),
        _outcome("2", "hard", False),
        _outcome("3", "hard", None, flags=["no_key_entry"]),
    ]
    s = summarize(outcomes)
    assert s["gradable"] == 2
    assert s["ungradable"] == 1
    assert s["accuracy"] == 50.0
    assert s["by_level"]["easy"]["accuracy"] == 100.0
    assert s["by_level"]["hard"]["accuracy"] == 0.0


def test_unanswered_counts_as_wrong_when_gradable():
    o = _outcome("1", answer=None, correct=False, flags=["no_final_answer"])
    s = summarize([o])
    assert s["accuracy"] == 0.0
    assert s["unanswered"] == 1


def test_find_regressions_only_correct_to_wrong():
    prev = [_outcome("1", correct=True), _outcome("2", correct=False)]
    cur = [_outcome("1", correct=False), _outcome("2", correct=None)]
    assert find_regressions(prev, cur) == ["1"]


def test_render_failed_table_and_sections():
    text = render([
        _outcome("1", correct=True),
        _outcome("2", "hard", correct=False, answer="wrong | thing"),
        _outcome("3", correct=False, answer=None,
                 terminal_status="failed", error="HarnessError: boom"),
    ], run_id="dev-001")
    assert "Accuracy: **1/3**" in text  # 1 correct of 3 gradable
    assert "wrong \\| thing" in text
    assert "HarnessError: boom" in text


def test_render_regressions_first():
    prev = [_outcome("1", correct=True)]
    cur = [_outcome("1", correct=False)]
    text = render(cur, previous=prev, run_id="dev-002")
    assert text.index("## Regressions") < text.index("## Score")
