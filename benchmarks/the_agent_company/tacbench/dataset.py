"""Discover TheAgentCompany tasks from the vendored checkout.

Each ``workspaces/tasks/<name>/`` holds ``task.md`` (the instruction the
agent receives), ``checkpoints.md`` (human-readable grading spec, whose
header carries the total points) and ``evaluator.py`` (the programmatic
graders). Task instructions reference the company's services by the
canonical hostname ``the-agent-company.com``; :func:`instruction`
rewrites it to wherever the stack is actually hosted.
"""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass

from tacbench import vendor

CANONICAL_HOSTNAME = "the-agent-company.com"

_TOTAL_RE = re.compile(r"(\d+)\s*points?\s+in\s+total", re.IGNORECASE)
# Headers write points as "(1pt)", "(2pts)", "(1 point)" or "(3 points)".
_CHECKPOINT_RE = re.compile(
    r"^##\s*Checkpoint\s+\d+\s*\((\d+)\s*(?:pts?|points?)\)",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class Task:
    task_id: str
    task_md: str
    checkpoints_md: str
    total_points: int
    checkpoint_points: tuple[int, ...]
    evaluator_path: str
    task_dir: str


def parse_points(checkpoints_md: str) -> tuple[int, tuple[int, ...]]:
    """(total, per-checkpoint) from checkpoints.md; falls back to the sum
    of checkpoint headers when the total line is absent."""
    per = tuple(int(m) for m in _CHECKPOINT_RE.findall(checkpoints_md))
    total_match = _TOTAL_RE.search(checkpoints_md)
    total = int(total_match.group(1)) if total_match else sum(per)
    return total, per


def load_tasks(prefixes: tuple[str, ...] | None = None) -> list[Task]:
    root = vendor.home() / "workspaces" / "tasks"
    tasks: list[Task] = []
    for task_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if prefixes and not any(task_dir.name.startswith(p) for p in prefixes):
            continue
        task_md = task_dir / "task.md"
        checkpoints_md = task_dir / "checkpoints.md"
        evaluator = task_dir / "evaluator.py"
        if not task_md.is_file() or not evaluator.is_file():
            continue  # not a runnable task dir (helpers, templates)
        cp_text = checkpoints_md.read_text(encoding="utf-8") \
            if checkpoints_md.is_file() else ""
        total, per = parse_points(cp_text)
        tasks.append(Task(
            task_id=task_dir.name,
            task_md=task_md.read_text(encoding="utf-8"),
            checkpoints_md=cp_text,
            total_points=total,
            checkpoint_points=per,
            evaluator_path=str(evaluator),
            task_dir=str(task_dir),
        ))
    if not tasks:
        raise SystemExit(f"no runnable tasks under {root}")
    return tasks


def instruction(task: Task, hostname: str) -> str:
    """The agent-facing instruction, service hostname substituted."""
    return task.task_md.replace(CANONICAL_HOSTNAME, hostname)


def category(task: Task) -> str:
    """Leaderboard grouping: the task-name prefix (admin, hr, pm, sde,
    ds, finance, research...)."""
    return task.task_id.split("-", 1)[0]
