"""Aggregate graded TAC runs into the leaderboard's metric shape.

Upstream's headline score blends full completions with partial credit:
per task, ``score = 0.5 * full + 0.5 * earned/total`` (full = every
checkpoint passed), averaged over tasks and reported as a percentage.
Ungradable tasks are reported, never counted as zeros.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Any


@dataclass
class TaskOutcome:
    task_id: str
    category: str
    points_total: int | None
    points_earned: int | None
    terminal_status: str = ""
    error: str | None = None
    grade_error: str | None = None

    @property
    def gradable(self) -> bool:
        return self.points_total is not None and self.points_total > 0

    @property
    def full(self) -> bool:
        return self.gradable and self.points_earned == self.points_total

    @property
    def partial_score(self) -> float | None:
        if not self.gradable:
            return None
        ratio = (self.points_earned or 0) / self.points_total
        return 0.5 * (1.0 if self.full else 0.0) + 0.5 * ratio


def summarize(outcomes: list[TaskOutcome]) -> dict[str, Any]:
    graded = [o for o in outcomes if o.gradable]
    categories = sorted({o.category for o in outcomes})

    def block(rows: list[TaskOutcome]) -> dict[str, Any]:
        scores = [o.partial_score for o in rows if o.partial_score is not None]
        return {
            "tasks": len(rows),
            "full": sum(1 for o in rows if o.full),
            "score": round(100 * mean(scores), 2) if scores else None,
        }

    return {
        "tasks": len(outcomes),
        "graded": len(graded),
        "ungradable": len(outcomes) - len(graded),
        "overall": block(graded),
        "by_category": {
            c: block([o for o in graded if o.category == c])
            for c in categories
            if any(o.category == c for o in graded)
        },
    }


def render(outcomes: list[TaskOutcome], run_id: str = "") -> str:
    s = summarize(outcomes)
    out: list[str] = [f"# TheAgentCompany run {run_id}".rstrip(), ""]

    out.append("## Score")
    out.append("")
    o = s["overall"]
    out.append(
        f"- Score (upstream partial-credit formula): **{o['score']}** over "
        f"{s['graded']} graded task(s)"
    )
    out.append(f"- Full completions: **{o['full']}/{s['graded']}**")
    if s["ungradable"]:
        out.append(f"- Ungradable: {s['ungradable']} task(s) -- listed below")
    out.append("")

    out.append("| Category | Tasks | Full | Score |")
    out.append("| --- | --- | --- | --- |")
    for cat, row in s["by_category"].items():
        out.append(f"| {cat} | {row['tasks']} | {row['full']} | {row['score']} |")
    out.append("")

    failed = sorted(
        (x for x in outcomes if not x.full),
        key=lambda x: (x.category, x.task_id),
    )
    out.append("## Tasks below full completion")
    out.append("")
    if failed:
        out.append("| Task | Points | Status | Why (first signal) |")
        out.append("| --- | --- | --- | --- |")
        for x in failed:
            points = (f"{x.points_earned}/{x.points_total}"
                      if x.gradable else "--")
            why = (x.grade_error or x.error or "checkpoints missed")[:110]
            out.append(f"| `{x.task_id}` | {points} | {x.terminal_status} | {why} |")
    else:
        out.append("None.")
    out.append("")
    return "\n".join(out)
