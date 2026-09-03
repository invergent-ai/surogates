"""Aggregate judged runs into the metrics the leaderboard reports.

Definitions (matching the public Workspace-Bench card):
- Rubric Pass Rate: passed rubrics / total rubrics, micro-averaged over
  every judged task.
- Easy/Medium/Hard Rubrics Accuracy: the same ratio restricted to tasks
  of that difficulty.
- Pass@k (k in 50/60/80): fraction of tasks whose own rubric pass
  fraction is >= k%.

Regressions come first in the rendered report, above the score: a net +2
points can hide five tasks that broke. "Task regressed" means its Pass@60
flipped from true to false against the compared run.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

PASS_AT_THRESHOLDS = (50, 60, 80)


@dataclass
class TaskOutcome:
    task_id: str
    persona: str
    difficulty: str
    total_rubrics: int
    passed_rubrics: int
    # passed/total per rubric type, e.g. {"Outcome Evaluation": [3, 5]}.
    by_rubric_type: dict[str, list[int]] = field(default_factory=dict)
    missing_outputs: list[str] = field(default_factory=list)
    terminal_status: str = ""
    error: str | None = None
    judge_error: str | None = None

    @property
    def pass_fraction(self) -> float:
        return self.passed_rubrics / self.total_rubrics if self.total_rubrics else 0.0


def pass_at(outcome: TaskOutcome, threshold_pct: int) -> bool:
    return outcome.pass_fraction * 100 >= threshold_pct


def summarize(outcomes: list[TaskOutcome]) -> dict[str, Any]:
    total_rubrics = sum(o.total_rubrics for o in outcomes)
    passed_rubrics = sum(o.passed_rubrics for o in outcomes)

    def pct(passed: int, total: int) -> float:
        return round(100.0 * passed / total, 1) if total else 0.0

    by_difficulty: dict[str, dict[str, int]] = {}
    for o in outcomes:
        row = by_difficulty.setdefault(
            o.difficulty or "unknown", {"tasks": 0, "passed": 0, "total": 0}
        )
        row["tasks"] += 1
        row["passed"] += o.passed_rubrics
        row["total"] += o.total_rubrics

    by_type: dict[str, list[int]] = {}
    for o in outcomes:
        for rtype, (p, t) in o.by_rubric_type.items():
            agg = by_type.setdefault(rtype, [0, 0])
            agg[0] += p
            agg[1] += t

    pass_at_k = {
        k: round(
            100.0 * sum(1 for o in outcomes if pass_at(o, k)) / len(outcomes), 1
        ) if outcomes else 0.0
        for k in PASS_AT_THRESHOLDS
    }

    status_counts = Counter(
        o.terminal_status for o in outcomes if o.terminal_status != "completed"
    )
    return {
        "tasks": len(outcomes),
        "total_rubrics": total_rubrics,
        "passed_rubrics": passed_rubrics,
        "rubric_pass_rate": pct(passed_rubrics, total_rubrics),
        "by_difficulty": {
            d: {
                "tasks": row["tasks"],
                "accuracy": pct(row["passed"], row["total"]),
            }
            for d, row in by_difficulty.items()
        },
        "by_rubric_type": {
            rtype: pct(p, t) for rtype, (p, t) in sorted(by_type.items())
        },
        "pass_at": pass_at_k,
        "abnormal_statuses": dict(status_counts),
        "judge_errors": sum(1 for o in outcomes if o.judge_error),
        "missing_output_tasks": sum(1 for o in outcomes if o.missing_outputs),
    }


def find_regressions(
    previous: list[TaskOutcome], current: list[TaskOutcome]
) -> list[str]:
    """Task ids whose Pass@60 flipped from pass to fail."""
    was_passing = {o.task_id for o in previous if pass_at(o, 60)}
    now_failing = {o.task_id for o in current if not pass_at(o, 60)}
    return sorted(was_passing & now_failing, key=lambda x: (len(x), x))


def render(
    outcomes: list[TaskOutcome],
    previous: list[TaskOutcome] | None = None,
    run_id: str = "",
) -> str:
    s = summarize(outcomes)
    out: list[str] = [f"# Workspace-Bench run {run_id}".rstrip(), ""]

    if previous is not None:
        regressions = find_regressions(previous, outcomes)
        out.append("## Regressions")
        out.append("")
        if regressions:
            out.append(
                f"{len(regressions)} task(s) passed Pass@60 before and fail now:"
            )
            out.append("")
            for tid in regressions:
                out.append(f"- `{tid}`")
        else:
            out.append("None.")
        out.append("")
        out.append(
            "> Deltas from a single run are **provisional** -- the judge and "
            "the agent are both stochastic. Re-run the affected subset 3x "
            "before treating a fix (or a break) as real."
        )
        out.append("")

    out.append("## Score")
    out.append("")
    out.append(
        f"- Rubric Pass Rate: **{s['passed_rubrics']}/{s['total_rubrics']}** "
        f"(**{s['rubric_pass_rate']}%**) over {s['tasks']} task(s)"
    )
    for k in PASS_AT_THRESHOLDS:
        out.append(f"- Pass@{k}: {s['pass_at'][k]}%")
    out.append("")

    out.append("| Difficulty | Tasks | Rubrics accuracy |")
    out.append("| --- | --- | --- |")
    for diff in ("easy", "medium", "hard", "unknown"):
        if diff in s["by_difficulty"]:
            row = s["by_difficulty"][diff]
            out.append(f"| {diff} | {row['tasks']} | {row['accuracy']}% |")
    out.append("")

    if s["by_rubric_type"]:
        out.append("| Rubric type | Accuracy |")
        out.append("| --- | --- |")
        for rtype, acc in s["by_rubric_type"].items():
            out.append(f"| {rtype} | {acc}% |")
        out.append("")

    if s["abnormal_statuses"] or s["judge_errors"]:
        out.append("## Run health")
        out.append("")
        for status, count in sorted(s["abnormal_statuses"].items()):
            out.append(f"- `{status}`: {count} task(s)")
        if s["judge_errors"]:
            out.append(
                f"- judge errors: {s['judge_errors']} task(s) "
                "(scored 0 -- re-judge before comparing runs)"
            )
        out.append("")

    # The failed-task list is the product: it is the harness-improvement
    # backlog, and RESULTS.md rows copy it verbatim.
    failed = sorted(
        (o for o in outcomes if not pass_at(o, 60)),
        key=lambda o: o.pass_fraction,
    )
    out.append("## Failed tasks (below Pass@60)")
    out.append("")
    if failed:
        out.append("| Task | Difficulty | Rubrics | Status | Why (first signal) |")
        out.append("| --- | --- | --- | --- | --- |")
        for o in failed:
            why = (
                o.judge_error
                or o.error
                or (f"missing outputs: {', '.join(o.missing_outputs)}"
                    if o.missing_outputs else "")
                or "rubrics failed on content"
            )
            out.append(
                f"| `{o.task_id}` | {o.difficulty} | "
                f"{o.passed_rubrics}/{o.total_rubrics} | {o.terminal_status} | "
                f"{why[:120]} |"
            )
    else:
        out.append("None.")
    out.append("")

    return "\n".join(out)
