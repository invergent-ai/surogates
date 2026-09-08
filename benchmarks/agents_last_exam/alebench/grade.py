"""Grade collected outputs with the task's own grader script.

Every public ALE grader follows one convention: a Python script taking
``--pred-dir`` and ``--gt-dir`` and printing a JSON report whose
``total_score`` is the task's points. The grader runs locally, in this
benchmark's venv, as a subprocess -- the agent never sees the grader or
the references.

Graders import domain libraries (the task card's ``software`` list); a
grader that cannot run here (missing deps, divergent args) marks the
task **ungradable with the reason**, never zero -- install the domain's
packages into the venv and re-grade, rollouts are not re-run.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from dataclasses import dataclass, field

GRADER_TIMEOUT_S = 600


@dataclass
class GradeResult:
    task_id: str
    total_score: float | None
    report: dict = field(default_factory=dict)
    grade_error: str | None = None


def _extract_json(stdout: str) -> dict:
    text = stdout.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def grade_task(
    task_id: str,
    grader_script: str,
    pred_dir: str,
    reference_dir: str,
    python: str = sys.executable,
) -> GradeResult:
    if not pathlib.Path(pred_dir).is_dir() or not any(
        pathlib.Path(pred_dir).rglob("*")
    ):
        return GradeResult(
            task_id=task_id, total_score=0.0,
            report={"note": "agent produced no output files"},
        )
    try:
        proc = subprocess.run(
            [python, grader_script,
             "--pred-dir", pred_dir, "--gt-dir", reference_dir],
            capture_output=True, text=True, timeout=GRADER_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return GradeResult(task_id=task_id, total_score=None,
                           grade_error=f"grader timed out after {GRADER_TIMEOUT_S}s")

    if proc.returncode != 0:
        return GradeResult(
            task_id=task_id, total_score=None,
            grade_error=(proc.stderr.strip() or proc.stdout.strip()
                         or f"grader exited {proc.returncode}")[-400:],
        )
    try:
        report = _extract_json(proc.stdout)
    except json.JSONDecodeError:
        return GradeResult(
            task_id=task_id, total_score=None,
            grade_error=f"grader printed no JSON: {proc.stdout[:200]!r}",
        )

    score = report.get("total_score")
    if not isinstance(score, (int, float)):
        return GradeResult(
            task_id=task_id, total_score=None, report=report,
            grade_error="grader report has no numeric total_score",
        )
    return GradeResult(task_id=task_id, total_score=float(score), report=report)
