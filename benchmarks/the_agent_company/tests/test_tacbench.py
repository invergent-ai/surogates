"""Offline suite: isolation, task parsing, grading plumbing, report math."""
import json
import pathlib
import re
import subprocess

import pytest

from tacbench.cli import next_run_id
from tacbench.dataset import Task, category, instruction, load_tasks, parse_points
from tacbench.grade import build_command, grade_task, parse_result
from tacbench.report import TaskOutcome, render, summarize

PACKAGE_DIR = pathlib.Path(__file__).parent.parent / "tacbench"


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

CHECKPOINTS_MD = """# Checkpoints

This task has 3 points in total.

## Checkpoint 1 (1pt)

Something.

## Checkpoint 2 (2pts)

Something else.
"""


def test_parse_points_total_and_per_checkpoint():
    total, per = parse_points(CHECKPOINTS_MD)
    assert total == 3
    assert per == (1, 2)


def test_parse_points_falls_back_to_sum():
    total, per = parse_points("## Checkpoint 1 (2pts)\n## Checkpoint 2 (1pt)\n")
    assert total == 3 and per == (2, 1)


def _task(**overrides):
    fields = dict(
        task_id="admin-arrange-meeting-rooms",
        task_md="Visit http://the-agent-company.com:3000/home and do X.",
        checkpoints_md=CHECKPOINTS_MD,
        total_points=3,
        checkpoint_points=(1, 2),
        evaluator_path="/x/evaluator.py",
        task_dir="/x",
    )
    fields.update(overrides)
    return Task(**fields)


def test_instruction_substitutes_hostname():
    text = instruction(_task(), "10.0.0.5")
    assert "http://10.0.0.5:3000/home" in text
    assert "the-agent-company.com" not in text


def test_category_is_prefix():
    assert category(_task()) == "admin"
    assert category(_task(task_id="sde-fix-bug")) == "sde"


def test_load_tasks_discovers_dirs(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    for name in ("admin-a", "sde-b"):
        d = root / "workspaces" / "tasks" / name
        d.mkdir(parents=True)
        (d / "task.md").write_text("Do it.")
        (d / "checkpoints.md").write_text(CHECKPOINTS_MD)
        (d / "evaluator.py").write_text("# eval")
    (root / "workspaces" / "tasks" / "not-a-task").mkdir()
    monkeypatch.setenv("TAC_HOME", str(root))

    tasks = load_tasks()
    assert [t.task_id for t in tasks] == ["admin-a", "sde-b"]
    assert tasks[0].total_points == 3
    assert load_tasks(("sde",))[0].task_id == "sde-b"


# -- grading -----------------------------------------------------------

def test_build_command_shape():
    cmd = build_command("admin-a", "/runs/x/workspace", "10.0.0.5",
                        "ghcr.io/theagentcompany")
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert "--add-host" in cmd
    assert "the-agent-company.com:10.0.0.5" in cmd
    assert "/runs/x/workspace:/workspace:ro" in cmd
    assert "ghcr.io/theagentcompany/admin-a-image:latest" in cmd


def test_parse_result_reads_tacresult_line():
    stdout = (
        "some evaluator noise\n"
        'TACRESULT {"checkpoints": [{"total": 1, "result": 1}, '
        '{"total": 2, "result": 0}], "final": {"total": 3, "result": 1}}\n'
    )
    result = parse_result(stdout, "admin-a")
    assert result.points_total == 3
    assert result.points_earned == 1
    assert len(result.checkpoints) == 2
    assert result.grade_error is None


def test_parse_result_without_marker_is_ungradable():
    result = parse_result("crash log only", "admin-a")
    assert result.points_total is None
    assert "no TACRESULT" in result.grade_error


def test_grade_task_with_fake_docker():
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout='TACRESULT {"checkpoints": [], "final": {"total": 2, "result": 2}}',
            stderr="",
        )

    result = grade_task("admin-a", "/w", "10.0.0.5", "reg", runner=fake_run)
    assert result.points_earned == 2


def test_grade_task_failure_is_ungradable():
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 125, stdout="",
                                           stderr="no such image")

    result = grade_task("admin-a", "/w", "10.0.0.5", "reg", runner=fake_run)
    assert result.points_total is None
    assert "no such image" in result.grade_error


# -- report ------------------------------------------------------------

def test_partial_credit_formula():
    full = TaskOutcome("a", "admin", 2, 2)
    half = TaskOutcome("b", "admin", 2, 1)
    zero = TaskOutcome("c", "sde", 2, 0)
    bad = TaskOutcome("d", "sde", None, None, grade_error="x")
    assert full.partial_score == 1.0
    assert half.partial_score == 0.25
    assert zero.partial_score == 0.0
    assert bad.partial_score is None

    s = summarize([full, half, zero, bad])
    assert s["graded"] == 3 and s["ungradable"] == 1
    assert s["overall"]["full"] == 1
    assert s["overall"]["score"] == round(100 * (1.0 + 0.25 + 0.0) / 3, 2)


def test_render_lists_incomplete_tasks():
    text = render([
        TaskOutcome("admin-a", "admin", 2, 2),
        TaskOutcome("sde-b", "sde", 3, 1, terminal_status="completed"),
        TaskOutcome("hr-c", "hr", None, None, grade_error="docker not found"),
    ], run_id="smoke-001")
    assert "`sde-b`" in text and "1/3" in text
    assert "docker not found" in text
    assert "| `admin-a` |" not in text


# -- cli ---------------------------------------------------------------

def test_next_run_id_sequences_per_prefix(tmp_path):
    (tmp_path / "full-004").mkdir()
    (tmp_path / "smoke-002").mkdir()
    (tmp_path / "stray.txt").write_text("")
    assert next_run_id(tmp_path, "full") == "full-005"
    assert next_run_id(tmp_path, "smoke") == "smoke-003"
