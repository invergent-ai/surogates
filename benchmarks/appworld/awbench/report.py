"""Aggregate evaluated AppWorld runs.

Headline is upstream's Task Goal Completion: the fraction of tasks
whose full stateful test suite passed. Unevaluable tasks (evaluation
crashed, world never bound) are reported, never counted as failures
silently.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class TaskOutcome:
    task_id: str
    passed: bool | None  # None = unevaluable
    terminal_status: str = ""
    error: str | None = None
    evaluate_error: str | None = None
    failures: int = 0
    wall_clock_s: float = 0.0


def summarize(outcomes: list[TaskOutcome]) -> dict[str, Any]:
    evaluable = [o for o in outcomes if o.passed is not None]
    return {
        "tasks": len(outcomes),
        "evaluable": len(evaluable),
        "unevaluable": len(outcomes) - len(evaluable),
        "passed": sum(1 for o in evaluable if o.passed),
        "tgc": round(100.0 * sum(1 for o in evaluable if o.passed)
                     / len(evaluable), 1) if evaluable else None,
    }


def render(outcomes: list[TaskOutcome], run_id: str = "") -> str:
    s = summarize(outcomes)
    out: list[str] = [f"# AppWorld run {run_id}".rstrip(), ""]

    out.append("## Score")
    out.append("")
    out.append(
        f"- Task Goal Completion: **{s['passed']}/{s['evaluable']}** "
        f"(**{s['tgc'] if s['tgc'] is not None else '--'}%**)"
    )
    if s["unevaluable"]:
        out.append(f"- Unevaluable: {s['unevaluable']} task(s) -- listed below")
    out.append("")

    failed = sorted(
        (o for o in outcomes if o.passed is not True),
        key=lambda o: o.task_id,
    )
    out.append("## Failed tasks")
    out.append("")
    if failed:
        out.append("| Task | Failed tests | Status | Why (first signal) |")
        out.append("| --- | --- | --- | --- |")
        for o in failed:
            why = (o.evaluate_error or o.error or "task tests failed")[:110]
            out.append(f"| `{o.task_id}` | {o.failures or '--'} | "
                       f"{o.terminal_status} | {why} |")
    else:
        out.append("None.")
    out.append("")
    return "\n".join(out)
