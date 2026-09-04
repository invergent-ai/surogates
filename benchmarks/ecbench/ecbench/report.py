"""Aggregate scored episodes into the run report.

The primary metric is upstream's: end-of-horizon total assets (and its
multiple of the opening stake). Everything is deterministic arithmetic
over ``outcomes.json`` -- episode-to-episode variance is entirely the
agent's, which is why counted runs use several episodes and report the
mean alongside the spread.
"""
from __future__ import annotations

from dataclasses import asdict  # noqa: F401  (re-exported for cli)
from statistics import mean, median
from typing import Any

from ecbench.scorer import EpisodeOutcome


def summarize(outcomes: list[EpisodeOutcome]) -> dict[str, Any]:
    scored = [o for o in outcomes if o.final_assets is not None]
    assets = [o.final_assets for o in scored]
    multiples = [o.multiple for o in scored if o.multiple is not None]
    return {
        "episodes": len(outcomes),
        "scored": len(scored),
        "unscoreable": len(outcomes) - len(scored),
        "mean_assets": round(mean(assets), 2) if assets else None,
        "median_assets": round(median(assets), 2) if assets else None,
        "min_assets": round(min(assets), 2) if assets else None,
        "max_assets": round(max(assets), 2) if assets else None,
        "mean_multiple": round(mean(multiples), 3) if multiples else None,
        "full_horizon": sum(1 for o in scored if o.done),
        "mean_days": round(mean(o.days_completed for o in scored), 1)
        if scored else None,
    }


def render(outcomes: list[EpisodeOutcome], run_id: str = "") -> str:
    s = summarize(outcomes)
    out: list[str] = [f"# ECBench run {run_id}".rstrip(), ""]

    out.append("## Score")
    out.append("")
    if s["scored"]:
        out.append(
            f"- Mean final assets: **{s['mean_assets']:,.0f}** "
            f"(**{s['mean_multiple']}x** the stake) over "
            f"{s['scored']} scored episode(s)"
        )
        out.append(
            f"- Median {s['median_assets']:,.0f}; range "
            f"{s['min_assets']:,.0f} .. {s['max_assets']:,.0f}"
        )
        out.append(
            f"- Full horizon reached: {s['full_horizon']}/{s['scored']} "
            f"(mean {s['mean_days']} days)"
        )
    else:
        out.append("- No scoreable episodes.")
    if s["unscoreable"]:
        out.append(f"- Unscoreable episodes: **{s['unscoreable']}**")
    out.append("")

    out.append("| Episode | Assets | Multiple | Days | Batches | Status | Source | Notes |")
    out.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for o in sorted(outcomes, key=lambda x: x.episode):
        assets = f"{o.final_assets:,.0f}" if o.final_assets is not None else "--"
        multiple = f"{o.multiple}x" if o.multiple is not None else "--"
        note = "; ".join(o.notes)[:110] or (o.error or "")[:110]
        out.append(
            f"| {o.episode:02d} | {assets} | {multiple} | "
            f"{o.days_completed}/{o.max_days or '?'} | {o.tool_batches} | "
            f"{o.terminal_status} | {o.source} | {note} |"
        )
    out.append("")
    return "\n".join(out)
