"""Aggregate graded runs into the metrics the leaderboard reports.

Accuracy overall and per level (easy/hard), computed over *gradable*
tasks -- those with a derived-key entry. Ungradable tasks (no public
correct answer exists yet) are reported, never silently dropped and
never counted as failures. Regressions come first when comparing runs:
a task that flipped correct -> incorrect is worth more attention than a
net score delta.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskOutcome:
    task_id: str
    level: str
    answer: str | None  # what the agent said (None = no FINAL ANSWER)
    correct: bool | None  # None = ungradable (no key entry)
    key_source: str = ""  # task_scores | upstream-dev | ""
    terminal_status: str = ""
    error: str | None = None
    flags: list[str] = field(default_factory=list)


def summarize(outcomes: list[TaskOutcome]) -> dict[str, Any]:
    gradable = [o for o in outcomes if o.correct is not None]

    def acc(rows: list[TaskOutcome]) -> float | None:
        if not rows:
            return None
        return round(100.0 * sum(o.correct for o in rows) / len(rows), 1)

    by_level = {
        level: {
            "total": len(rows),
            "correct": sum(o.correct for o in rows),
            "accuracy": acc(rows),
        }
        for level in ("easy", "hard")
        if (rows := [o for o in gradable if o.level == level])
    }
    flag_counts = Counter(f for o in outcomes for f in o.flags)
    return {
        "tasks": len(outcomes),
        "gradable": len(gradable),
        "ungradable": len(outcomes) - len(gradable),
        "correct": sum(o.correct for o in gradable),
        "accuracy": acc(gradable),
        "by_level": by_level,
        "unanswered": sum(1 for o in outcomes if o.answer is None),
        "flags": dict(flag_counts),
    }


def find_regressions(
    previous: list[TaskOutcome], current: list[TaskOutcome]
) -> list[str]:
    was_correct = {o.task_id for o in previous if o.correct}
    now_wrong = {o.task_id for o in current if o.correct is False}
    return sorted(was_correct & now_wrong, key=lambda x: (len(x), x))


def render(
    outcomes: list[TaskOutcome],
    previous: list[TaskOutcome] | None = None,
    run_id: str = "",
) -> str:
    s = summarize(outcomes)
    out: list[str] = [f"# DABstep run {run_id}".rstrip(), ""]

    if previous is not None:
        regressions = find_regressions(previous, outcomes)
        out.append("## Regressions")
        out.append("")
        if regressions:
            out.append(f"{len(regressions)} task(s) were correct before and wrong now:")
            out.append("")
            for tid in regressions:
                out.append(f"- `{tid}`")
        else:
            out.append("None.")
        out.append("")
        out.append(
            "> Single-run deltas are **provisional** -- re-run the affected "
            "subset 3x before treating a fix (or a break) as real."
        )
        out.append("")

    out.append("## Score")
    out.append("")
    out.append(
        f"- Accuracy: **{s['correct']}/{s['gradable']}** "
        f"(**{s['accuracy'] if s['accuracy'] is not None else '--'}%**) "
        "over gradable tasks"
    )
    for level in ("easy", "hard"):
        if level in s["by_level"]:
            row = s["by_level"][level]
            out.append(
                f"- {level.capitalize()}: {row['correct']}/{row['total']} "
                f"({row['accuracy']}%)"
            )
    if s["ungradable"]:
        out.append(
            f"- Ungradable (no key entry yet): {s['ungradable']} task(s) -- "
            "excluded from accuracy"
        )
    out.append(f"- No FINAL ANSWER: {s['unanswered']} task(s)")
    out.append("")

    if s["flags"]:
        out.append("| Flag | Count |")
        out.append("| --- | --- |")
        for flag, count in sorted(s["flags"].items(), key=lambda kv: -kv[1]):
            out.append(f"| `{flag}` | {count} |")
        out.append("")

    failed = sorted(
        (o for o in outcomes if o.correct is False),
        key=lambda o: (o.level, (len(o.task_id), o.task_id)),
    )
    out.append("## Failed tasks")
    out.append("")
    if failed:
        out.append("| Task | Level | Agent answer | Status | Why (first signal) |")
        out.append("| --- | --- | --- | --- | --- |")
        for o in failed:
            why = o.error or ("no FINAL ANSWER" if o.answer is None else "wrong answer")
            answer = (o.answer or "--").replace("|", "\\|")[:60]
            out.append(
                f"| `{o.task_id}` | {o.level} | {answer} | "
                f"{o.terminal_status} | {why[:100]} |"
            )
    else:
        out.append("None.")
    out.append("")
    return "\n".join(out)
