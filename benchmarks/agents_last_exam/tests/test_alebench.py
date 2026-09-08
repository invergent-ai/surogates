"""Offline suite: isolation, task discovery, grading, run-id sequencing."""
import json
import pathlib
import re
import textwrap

import pytest

from alebench.cli import next_run_id
from alebench.dataset import Task, eligibility, load_tasks, staged_files
from alebench.grade import grade_task
from alebench.report import TaskOutcome, render, summarize

PACKAGE_DIR = pathlib.Path(__file__).parent.parent / "alebench"


# -- isolation ---------------------------------------------------------

def test_no_product_imports():
    offenders = []
    for path in PACKAGE_DIR.rglob("*.py"):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if re.match(r"\s*(import|from)\s+(surogate_ops|surogates)\b", line):
                offenders.append(f"{path.name}:{lineno}")
    assert offenders == []


# -- dataset -----------------------------------------------------------

@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    task_dir = root / "tasks" / "demo" / "hello_task"
    (task_dir / "scripts").mkdir(parents=True)
    (task_dir / "task_card.json").write_text(json.dumps({
        "taskId": "demo/hello_task",
        "title": "Hello",
        "taskPrompt": "Do the thing.",
        "agentMustDo": ["Produce output/answer.txt"],
        "software": ["Python"],
    }))
    (task_dir / "scripts" / "score_outputs.py").write_text("# grader")
    data = tmp_path / "data" / "demo" / "hello_task"
    (data / "input").mkdir(parents=True)
    (data / "output").mkdir(parents=True)
    (data / "input" / "brief.md").write_text("hello")
    (data / "output" / "answer.txt").write_text("42")
    monkeypatch.setenv("ALE_HOME", str(root))
    monkeypatch.setenv("ALE_DATA_DIR", str(tmp_path / "data"))
    return root


def test_load_tasks_and_eligibility(fake_home):
    [task] = load_tasks()
    assert task.task_id == "demo/hello_task"
    assert task.grader_script.endswith("score_outputs.py")
    assert eligibility(task) is None
    assert staged_files(task) == [
        (task.input_dir + "/brief.md", "input/brief.md")
    ]


def test_missing_data_is_ineligible(fake_home, monkeypatch, tmp_path):
    monkeypatch.setenv("ALE_DATA_DIR", str(tmp_path / "nowhere"))
    [task] = load_tasks()
    assert "data not present" in eligibility(task)


def test_task_without_grader_is_ineligible(fake_home):
    grader = pathlib.Path(load_tasks()[0].grader_script)
    grader.unlink()
    [task] = load_tasks()
    assert "no grader script" in eligibility(task)


# -- grading -----------------------------------------------------------

GRADER = textwrap.dedent("""\
    import argparse, json, pathlib
    p = argparse.ArgumentParser()
    p.add_argument("--pred-dir", required=True)
    p.add_argument("--gt-dir", required=True)
    a = p.parse_args()
    pred = pathlib.Path(a.pred_dir) / "answer.txt"
    gt = pathlib.Path(a.gt_dir) / "answer.txt"
    ok = pred.exists() and pred.read_text() == gt.read_text()
    print(json.dumps({"total_score": 10.0 if ok else 0.0}))
""")


def _grade_setup(tmp_path, answer: str | None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    grader = tmp_path / "grader.py"
    grader.write_text(GRADER)
    gt = tmp_path / "gt"
    gt.mkdir()
    (gt / "answer.txt").write_text("42")
    pred = tmp_path / "pred"
    pred.mkdir()
    if answer is not None:
        (pred / "answer.txt").write_text(answer)
    return str(grader), str(pred), str(gt)


def test_grade_correct_and_wrong(tmp_path):
    grader, pred, gt = _grade_setup(tmp_path, "42")
    assert grade_task("t", grader, pred, gt).total_score == 10.0
    grader, pred, gt = _grade_setup(tmp_path / "b", "41")
    assert grade_task("t", grader, pred, gt).total_score == 0.0


def test_grade_empty_pred_short_circuits(tmp_path):
    grader, pred, gt = _grade_setup(tmp_path, None)
    result = grade_task("t", grader, pred, gt)
    assert result.total_score == 0.0
    assert "no output files" in result.report["note"]


def test_grade_crashing_grader_is_ungradable(tmp_path):
    grader = tmp_path / "boom.py"
    grader.write_text("import nonexistent_module_xyz")
    pred = tmp_path / "pred"
    pred.mkdir()
    (pred / "x").write_text("x")
    result = grade_task("t", str(grader), str(pred), str(tmp_path))
    assert result.total_score is None
    assert "nonexistent_module_xyz" in result.grade_error


# -- report ------------------------------------------------------------

def test_summarize_separates_ungradable():
    outcomes = [
        TaskOutcome("d/a", "d", 10.0, collected_files=2),
        TaskOutcome("d/b", "d", 0.0, collected_files=0),
        TaskOutcome("d/c", "d", None, grade_error="deps"),
    ]
    s = summarize(outcomes)
    assert s["graded"] == 2 and s["ungradable"] == 1
    assert s["positive"] == 1
    assert s["no_output"] == 1
    text = render(outcomes, run_id="smoke-001")
    assert "`d/c`" in text and "deps" in text


# -- cli ---------------------------------------------------------------

def test_next_run_id_sequences_per_prefix(tmp_path):
    (tmp_path / "full-002").mkdir()
    (tmp_path / "smoke-001").mkdir()
    (tmp_path / ".DS_Store").write_text("")
    assert next_run_id(tmp_path, "full") == "full-003"
    assert next_run_id(tmp_path, "smoke") == "smoke-002"
