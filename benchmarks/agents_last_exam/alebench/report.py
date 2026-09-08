"""Aggregate graded ALE runs.

Per-task grader reports carry ``total_score`` in task-specific point
scales, so cross-task averaging of raw points is meaningless. The
headline is therefore counts: graded tasks, tasks with a positive
score, and per-domain breakdowns -- with the per-task scores listed for
reading. Ungradable tasks (grader deps missing here) are reported,
never counted as zeros.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskOutcome:
    task_id: str
    domain: str
    total_score: float | None  # None = ungradable
    scored_positive: bool | None = None
    terminal_status: str = ""
    error: str | None = None
    grade_error: str | None = None
    collected_files: int = 0

    def __post_init__(self) -> None:
        if self.total_score is not None and self.scored_positive is None:
            self.scored_positive = self.total_score > 0


def summarize(outcomes: list[TaskOutcome]) -> dict[str, Any]:
    graded = [o for o in outcomes if o.total_score is not None]
    domains = sorted({o.domain for o in outcomes})
    return {
        "tasks": len(outcomes),
        "graded": len(graded),
        "ungradable": len(outcomes) - len(graded),
        "positive": sum(1 for o in graded if o.scored_positive),
        "no_output": sum(1 for o in graded if not o.collected_files),
        "by_domain": {
            d: {
                "tasks": len(rows),
                "graded": len([o for o in rows if o.total_score is not None]),
                "positive": sum(1 for o in rows if o.scored_positive),
            }
            for d in domains
            if (rows := [o for o in outcomes if o.domain == d])
        },
    }


def render(outcomes: list[TaskOutcome], run_id: str = "") -> str:
    s = summarize(outcomes)
    out: list[str] = [f"# Agents' Last Exam run {run_id}".rstrip(), ""]

    out.append("## Score")
    out.append("")
    out.append(
        f"- Graded: **{s['graded']}/{s['tasks']}** task(s); positive score "
        f"on **{s['positive']}** of them"
    )
    out.append(f"- Agent produced no output files: {s['no_output']} task(s)")
    if s["ungradable"]:
        out.append(f"- Ungradable here (grader deps/errors): "
                   f"{s['ungradable']} task(s) -- see below, then install "
                   "the domain packages and re-grade")
    out.append("")

    out.append("| Domain | Tasks | Graded | Positive |")
    out.append("| --- | --- | --- | --- |")
    for domain, row in s["by_domain"].items():
        out.append(f"| {domain} | {row['tasks']} | {row['graded']} | "
                   f"{row['positive']} |")
    out.append("")

    out.append("## Per-task scores")
    out.append("")
    out.append("| Task | Score | Files | Status | Note |")
    out.append("| --- | --- | --- | --- | --- |")
    for o in sorted(outcomes, key=lambda x: x.task_id):
        score = f"{o.total_score:g}" if o.total_score is not None else "--"
        note = (o.grade_error or o.error or "")[:100]
        out.append(f"| `{o.task_id}` | {score} | {o.collected_files} | "
                   f"{o.terminal_status} | {note} |")
    out.append("")
    return "\n".join(out)
