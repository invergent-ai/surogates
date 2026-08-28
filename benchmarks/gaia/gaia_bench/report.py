"""Render a run into markdown.

Regressions come first, above the score. A change that fixes browsing can
break file reading, and a net +3 hides a +8/-5.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskOutcome:
    task_id: str
    level: int
    strict_pass: bool
    lenient_pass: bool
    flags: list[str] = field(default_factory=list)
    root_cause: str | None = None
    owner: str | None = None
    # The classifier's justification and suggested fix. Persisted because a
    # verdict you cannot check is a verdict you cannot trust -- without the
    # cited trajectory step, auditing a ranking means re-reading raw traces.
    # Defaulted so outcomes.json files written before this existed still load.
    evidence: str = ""
    hypothesis: str = ""


def summarize(outcomes: list[TaskOutcome]) -> dict[str, Any]:
    total = len(outcomes)
    strict = sum(1 for o in outcomes if o.strict_pass)
    lenient = sum(1 for o in outcomes if o.lenient_pass)

    by_level: dict[int, dict[str, int]] = {}
    for o in outcomes:
        row = by_level.setdefault(o.level, {"total": 0, "strict_passed": 0})
        row["total"] += 1
        row["strict_passed"] += int(o.strict_pass)

    owner_counts = Counter(
        o.owner for o in outcomes
        if not o.strict_pass and o.owner and o.owner != "benchmark"
    )
    fix_list = [
        {
            "owner": owner,
            "count": count,
            "recoverable_pct": round(100.0 * count / total, 1) if total else 0.0,
        }
        for owner, count in owner_counts.most_common()
    ]

    flag_counts = Counter(f for o in outcomes for f in o.flags)
    ceiling = sum(
        1 for o in outcomes if o.root_cause == "ambiguous_ground_truth"
    )

    return {
        "total": total,
        "strict_passed": strict,
        "lenient_passed": lenient,
        "strict_pct": round(100.0 * strict / total, 1) if total else 0.0,
        "lenient_pct": round(100.0 * lenient / total, 1) if total else 0.0,
        "formatting_gap": lenient - strict,
        "by_level": by_level,
        "fix_list": fix_list,
        "flags": dict(flag_counts),
        "benchmark_ceiling": ceiling,
    }


def find_regressions(
    previous: list[TaskOutcome], current: list[TaskOutcome]
) -> list[str]:
    """Task ids that passed before and fail now."""
    was_passing = {o.task_id for o in previous if o.strict_pass}
    now_failing = {o.task_id for o in current if not o.strict_pass}
    return sorted(was_passing & now_failing)


def render(
    outcomes: list[TaskOutcome],
    previous: list[TaskOutcome] | None = None,
    run_id: str = "",
    provisional: bool = True,
) -> str:
    s = summarize(outcomes)
    out: list[str] = [f"# GAIA run {run_id}".rstrip(), ""]

    if previous is not None:
        regressions = find_regressions(previous, outcomes)
        out.append("## Regressions")
        out.append("")
        if regressions:
            out.append(
                f"{len(regressions)} task(s) passed before and fail now:"
            )
            out.append("")
            for tid in regressions:
                out.append(f"- `{tid}`")
        else:
            out.append("None.")
        out.append("")
        if provisional:
            out.append(
                "> Deltas from a single run are **provisional** -- tasks flip "
                "on their own. Re-run the affected subset 3x before treating "
                "a fix as real."
            )
            out.append("")

    out.append("## Score")
    out.append("")
    out.append(f"- Strict: **{s['strict_passed']}/{s['total']}** ({s['strict_pct']}%)")
    out.append(f"- Lenient: {s['lenient_passed']}/{s['total']} ({s['lenient_pct']}%)")
    out.append(
        f"- Formatting gap: **{s['formatting_gap']}** task(s) correct but "
        "mis-formatted -- a prompt fix, not a capability fix."
    )
    out.append("")
    out.append("| Level | Passed | Total |")
    out.append("| --- | --- | --- |")
    for level in sorted(s["by_level"]):
        row = s["by_level"][level]
        out.append(f"| {level} | {row['strict_passed']} | {row['total']} |")
    out.append("")

    out.append("## Failure flags")
    out.append("")
    if s["flags"]:
        out.append("| Flag | Count |")
        out.append("| --- | --- |")
        for flag, count in sorted(s["flags"].items(), key=lambda kv: -kv[1]):
            out.append(f"| `{flag}` | {count} |")
    else:
        out.append("None.")
    out.append("")

    out.append("## Ranked fix list")
    out.append("")
    out.append(
        "Upper bound: assumes every attributed failure is fixable and that "
        "fixing it surfaces no new failure behind it. Ranks where to look; "
        "does not promise a score delta."
    )
    out.append("")
    if s["fix_list"]:
        out.append("| Component | Failures | Recoverable |")
        out.append("| --- | --- | --- |")
        for row in s["fix_list"]:
            out.append(
                f"| {row['owner']} | {row['count']} | {row['recoverable_pct']}% |"
            )
    else:
        out.append("No attributed failures.")
    out.append("")

    if s["benchmark_ceiling"]:
        out.append(
            f"**Benchmark ceiling:** {s['benchmark_ceiling']} task(s) marked "
            "`ambiguous_ground_truth` -- excluded from the fix list above."
        )
        out.append("")

    # Per-verdict justification. The ranking above is only as good as these,
    # and the classifier is itself an LLM -- spot-check before trusting it.
    attributed = [o for o in outcomes if o.root_cause]
    if attributed:
        out.append("## Attributed failures")
        out.append("")
        out.append("Spot-check these before acting on the ranking.")
        out.append("")
        for o in sorted(attributed, key=lambda x: (x.owner or "", x.task_id)):
            out.append(f"### `{o.task_id[:8]}` (level {o.level}) -- {o.root_cause}")
            out.append("")
            out.append(f"- **Owner:** {o.owner}")
            if o.evidence:
                out.append(f"- **Evidence:** {o.evidence}")
            if o.hypothesis:
                out.append(f"- **Hypothesis:** {o.hypothesis}")
            out.append("")

    return "\n".join(out)
