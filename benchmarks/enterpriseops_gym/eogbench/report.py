"""Aggregate verified runs into the leaderboard's metric shape.

Task Success Rate: a task passes when every one of its hidden SQL
verifiers passes (upstream's definition), reported overall and per
domain. Unverifiable tasks (gym unreachable at verify time) are
reported, never counted as failures silently.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskOutcome:
    task_id: str
    domain: str
    passed: bool | None  # None = unverifiable
    verifiers_total: int
    verifiers_passed: int
    terminal_status: str = ""
    error: str | None = None
    verify_error: str | None = None
    failed_verifiers: list[str] = field(default_factory=list)


def summarize(outcomes: list[TaskOutcome]) -> dict[str, Any]:
    verifiable = [o for o in outcomes if o.passed is not None]

    def rate(rows: list[TaskOutcome]) -> float | None:
        if not rows:
            return None
        return round(100.0 * sum(o.passed for o in rows) / len(rows), 1)

    domains = sorted({o.domain for o in outcomes})
    return {
        "tasks": len(outcomes),
        "verifiable": len(verifiable),
        "unverifiable": len(outcomes) - len(verifiable),
        "passed": sum(1 for o in verifiable if o.passed),
        "success_rate": rate(verifiable),
        "by_domain": {
            d: {
                "tasks": len(rows),
                "passed": sum(1 for o in rows if o.passed),
                "success_rate": rate(rows),
            }
            for d in domains
            if (rows := [o for o in verifiable if o.domain == d])
        },
    }


def render(outcomes: list[TaskOutcome], run_id: str = "") -> str:
    s = summarize(outcomes)
    out: list[str] = [f"# EnterpriseOps-Gym run {run_id}".rstrip(), ""]

    out.append("## Score")
    out.append("")
    out.append(
        f"- Task Success Rate: **{s['passed']}/{s['verifiable']}** "
        f"(**{s['success_rate'] if s['success_rate'] is not None else '--'}%**)"
    )
    if s["unverifiable"]:
        out.append(f"- Unverifiable: {s['unverifiable']} task(s) -- "
                   "excluded from the rate, listed below")
    out.append("")

    out.append("| Domain | Tasks | Passed | Success rate |")
    out.append("| --- | --- | --- | --- |")
    for domain, row in s["by_domain"].items():
        out.append(f"| {domain} | {row['tasks']} | {row['passed']} | "
                   f"{row['success_rate']}% |")
    out.append("")

    failed = sorted(
        (o for o in outcomes if o.passed is not True),
        key=lambda o: (o.domain, o.task_id),
    )
    out.append("## Failed tasks")
    out.append("")
    if failed:
        out.append("| Task | Verifiers | Status | Why (first signal) |")
        out.append("| --- | --- | --- | --- |")
        for o in failed:
            why = (o.verify_error or o.error
                   or ("failed: " + ", ".join(o.failed_verifiers)
                       if o.failed_verifiers else "verifiers failed"))
            out.append(
                f"| `{o.task_id}` | {o.verifiers_passed}/{o.verifiers_total} "
                f"| {o.terminal_status} | {why[:110]} |"
            )
    else:
        out.append("None.")
    out.append("")
    return "\n".join(out)
