"""Aggregate scored AutomationBench runs.

Headline is upstream's official pass rate: mean of the strict
``task_completed_correctly`` over scored tasks. ``partial_credit``
(fraction of assertions satisfied) rides along as the denser signal.
Unscored tasks (rubric crashed) are reported, never counted as zeros.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Any


@dataclass
class TaskOutcome:
    task_id: str
    domain: str
    partial_credit: float | None
    strict: float | None
    tool_calls: int = 0
    completed_marker: bool = False
    terminal_status: str = ""
    error: str | None = None
    score_error: str | None = None


def summarize(outcomes: list[TaskOutcome]) -> dict[str, Any]:
    scored = [o for o in outcomes if o.strict is not None]
    domains = sorted({o.domain for o in outcomes})

    def block(rows: list[TaskOutcome]) -> dict[str, Any]:
        return {
            "tasks": len(rows),
            "passed": sum(1 for o in rows if o.strict == 1.0),
            "pass_rate": round(100 * mean(o.strict for o in rows), 1)
            if rows else None,
            "partial_credit": round(mean(o.partial_credit or 0.0
                                         for o in rows), 3) if rows else None,
        }

    return {
        "tasks": len(outcomes),
        "scored": len(scored),
        "unscored": len(outcomes) - len(scored),
        "no_tool_calls": sum(1 for o in outcomes if not o.tool_calls),
        "overall": block(scored),
        "by_domain": {
            d: block([o for o in scored if o.domain == d])
            for d in domains
            if any(o.domain == d for o in scored)
        },
    }


def render(outcomes: list[TaskOutcome], run_id: str = "") -> str:
    s = summarize(outcomes)
    o = s["overall"]
    out: list[str] = [f"# AutomationBench run {run_id}".rstrip(), ""]

    out.append("## Score")
    out.append("")
    out.append(
        f"- Pass rate (strict): **{o['passed']}/{s['scored']}** "
        f"(**{o['pass_rate'] if o['pass_rate'] is not None else '--'}%**)"
    )
    out.append(f"- Mean partial credit: {o['partial_credit']}")
    out.append(f"- Tasks with zero tool calls: {s['no_tool_calls']}")
    if s["unscored"]:
        out.append(f"- Unscored (rubric error): {s['unscored']} task(s)")
    out.append("")

    out.append("| Domain | Tasks | Passed | Pass rate | Partial credit |")
    out.append("| --- | --- | --- | --- | --- |")
    for domain, row in s["by_domain"].items():
        out.append(f"| {domain} | {row['tasks']} | {row['passed']} | "
                   f"{row['pass_rate']}% | {row['partial_credit']} |")
    out.append("")

    failed = sorted(
        (x for x in outcomes if x.strict != 1.0),
        key=lambda x: (x.domain, -(x.partial_credit or 0.0), x.task_id),
    )
    out.append("## Failed tasks")
    out.append("")
    if failed:
        out.append("| Task | Partial | Calls | Status | Why (first signal) |")
        out.append("| --- | --- | --- | --- | --- |")
        for x in failed:
            partial = f"{x.partial_credit:.2f}" if x.partial_credit is not None else "--"
            why = (x.score_error or x.error
                   or ("no tool calls made" if not x.tool_calls
                       else "assertions failed"))[:110]
            out.append(f"| `{x.task_id}` | {partial} | {x.tool_calls} | "
                       f"{x.terminal_status} | {why} |")
    else:
        out.append("None.")
    out.append("")
    return "\n".join(out)
