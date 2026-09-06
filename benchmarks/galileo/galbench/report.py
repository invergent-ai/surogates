"""Aggregate judged runs into the leaderboard's metric shape.

AC (Action Completion): per scenario, the fraction of user goals
accomplished; reported as the macro average, overall and per domain --
the leaderboard's headline. TSQ (Tool Selection Quality): fraction of
good tool calls, macro-averaged over scenarios that made any. Turn
counts ride along like upstream's Avg Turns.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean
from typing import Any


@dataclass
class ScenarioOutcome:
    scenario_id: str
    domain: str
    goals_total: int
    goals_done: int
    calls_total: int
    calls_good: int
    user_turns: int = 0
    agent_messages: int = 0
    completed_marker: bool = False
    terminal_status: str = ""
    error: str | None = None
    judge_error: str | None = None

    @property
    def ac(self) -> float:
        return self.goals_done / self.goals_total if self.goals_total else 0.0

    @property
    def tsq(self) -> float | None:
        if not self.calls_total:
            return None
        return self.calls_good / self.calls_total


def summarize(outcomes: list[ScenarioOutcome]) -> dict[str, Any]:
    def block(rows: list[ScenarioOutcome]) -> dict[str, Any]:
        tsqs = [o.tsq for o in rows if o.tsq is not None]
        return {
            "scenarios": len(rows),
            "ac": round(mean(o.ac for o in rows), 3) if rows else None,
            "tsq": round(mean(tsqs), 3) if tsqs else None,
            "avg_turns": round(mean(o.user_turns for o in rows), 1)
            if rows else None,
            "no_tool_calls": sum(1 for o in rows if not o.calls_total),
        }

    domains = sorted({o.domain for o in outcomes})
    summary = {
        "overall": block(outcomes),
        "by_domain": {d: block([o for o in outcomes if o.domain == d])
                      for d in domains},
        "completed_marker": sum(1 for o in outcomes if o.completed_marker),
        "abnormal": {
            s: n for s in ("failed", "timeout", "error")
            if (n := sum(1 for o in outcomes if o.terminal_status == s))
        },
        "judge_errors": sum(1 for o in outcomes if o.judge_error),
    }
    return summary


def find_regressions(
    previous: list[ScenarioOutcome], current: list[ScenarioOutcome]
) -> list[str]:
    """Scenarios whose AC dropped below 0.5 after being at or above it."""
    was_ok = {o.scenario_id for o in previous if o.ac >= 0.5}
    now_bad = {o.scenario_id for o in current if o.ac < 0.5}
    return sorted(was_ok & now_bad)


def render(
    outcomes: list[ScenarioOutcome],
    previous: list[ScenarioOutcome] | None = None,
    run_id: str = "",
) -> str:
    s = summarize(outcomes)
    out: list[str] = [f"# Agent Leaderboard run {run_id}".rstrip(), ""]

    if previous is not None:
        regressions = find_regressions(previous, outcomes)
        out.append("## Regressions")
        out.append("")
        if regressions:
            out.append(f"{len(regressions)} scenario(s) fell below AC 0.5:")
            out.append("")
            for sid in regressions:
                out.append(f"- `{sid}`")
        else:
            out.append("None.")
        out.append("")
        out.append(
            "> Single-run deltas are **provisional** -- the agent, both "
            "simulators and the judge are all stochastic surfaces. Re-run "
            "the affected subset 3x before treating a change as real."
        )
        out.append("")

    o = s["overall"]
    out.append("## Score")
    out.append("")
    out.append(
        f"- Action Completion (AC): **{o['ac']}** over {o['scenarios']} "
        "scenario(s) (macro avg of per-scenario goal fractions)"
    )
    out.append(f"- Tool Selection Quality (TSQ): **{o['tsq']}**")
    out.append(f"- Avg user turns: {o['avg_turns']}; scenarios with zero "
               f"tool calls: {o['no_tool_calls']}; CONVERSATION_COMPLETE "
               f"reached: {s['completed_marker']}/{o['scenarios']}")
    out.append("")

    out.append("| Domain | Scenarios | AC | TSQ | Avg turns | No-tool |")
    out.append("| --- | --- | --- | --- | --- | --- |")
    for domain, row in s["by_domain"].items():
        out.append(
            f"| {domain} | {row['scenarios']} | {row['ac']} | "
            f"{row['tsq'] if row['tsq'] is not None else '--'} | "
            f"{row['avg_turns']} | {row['no_tool_calls']} |"
        )
    out.append("")

    if s["abnormal"] or s["judge_errors"]:
        out.append("## Run health")
        out.append("")
        for status, count in s["abnormal"].items():
            out.append(f"- `{status}`: {count} scenario(s)")
        if s["judge_errors"]:
            out.append(f"- judge errors: {s['judge_errors']} scenario(s) "
                       "(scored 0 -- re-judge before comparing runs)")
        out.append("")

    failed = sorted(
        (o for o in outcomes if o.ac < 0.5),
        key=lambda o: (o.domain, o.ac, o.scenario_id),
    )
    out.append("## Failed scenarios (AC below 0.5)")
    out.append("")
    if failed:
        out.append("| Scenario | AC | TSQ | Turns | Calls | Status | Why (first signal) |")
        out.append("| --- | --- | --- | --- | --- | --- | --- |")
        for o in failed:
            why = (o.judge_error or o.error
                   or ("no tool calls made" if not o.calls_total else "goals not accomplished"))
            out.append(
                f"| `{o.scenario_id}` | {o.ac:.2f} | "
                f"{f'{o.tsq:.2f}' if o.tsq is not None else '--'} | "
                f"{o.user_turns} | {o.calls_total} | {o.terminal_status} | "
                f"{why[:100]} |"
            )
    else:
        out.append("None.")
    out.append("")
    return "\n".join(out)
