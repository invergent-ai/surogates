"""Research-kind mission judge policy.

Wraps the standard LLM rubric judge in deterministic gates:

* never evaluate while experiments are in flight (no verdict, no
  iteration burn);
* never honour ``satisfied`` without a machine-written held-out score
  (or an explicit budget-exhausted no-improvement close) AND a finished
  report task;
* demote terminal ``failed`` / ``blocked`` verdicts that lack
  deterministic corroboration, so a single noisy verdict cannot kill a
  long run.

These keep the rubric judge's per-run expressiveness and its
``needs_revision`` steering feedback while anchoring the terminal
decisions on machine-written state.
"""
from __future__ import annotations

from typing import Any

from surogates.arbor.store import is_improvement


async def research_should_skip(store: Any, run_id: Any) -> bool:
    """True while any experiment is still running — defer evaluation."""
    return (await store.in_flight_count(run_id)) > 0


def adjust_research_verdict(
    verdict: dict[str, Any], *, meta: dict[str, Any],
    report_task_done: bool, budget_exhausted: bool = False,
) -> dict[str, Any]:
    """Apply deterministic gates to the LLM judge's verdict."""
    result = verdict.get("result")

    if result == "satisfied":
        score = meta.get("test_trunk_score")
        improved = is_improvement(
            score, meta.get("test_baseline_score"),
            meta.get("metric_direction", "maximize"),
        )
        # A budget-exhausted run may close as satisfied with an explicit
        # no-improvement root insight even without a trunk gain.
        if not ((improved or budget_exhausted) and report_task_done):
            return {
                "result": "needs_revision",
                "explanation": "satisfied rejected by deterministic verification",
                "feedback": (
                    "satisfied requires a machine-written test_trunk_score "
                    "improving on test_baseline_score (or budget exhausted with "
                    "an explicit no-improvement root insight) AND the final "
                    "report task done. Merge through merge_experiment and "
                    "finalize with idea_tree(report) + a report task."
                ),
            }
        return verdict

    if result in ("failed", "blocked") and not budget_exhausted:
        return {
            "result": "needs_revision",
            "explanation": f"judge said {result} without deterministic corroboration",
            "feedback": verdict.get("feedback") or verdict.get("explanation") or "",
        }

    return verdict


def research_prompt_block(
    *, constraints_block: str, cycles_spent: int, max_cycles: int,
    convergence: str | None = None,
) -> str:
    """Tree-leaderboard block appended to the judge's user prompt for
    research missions (replaces the generic recent-tasks framing). When the
    run has plateaued, the convergence intervention is appended so the judge's
    needs_revision feedback can steer toward a paradigm shift or finalize."""
    block = (
        "## Research run state (machine-written; the ONLY trusted scores)\n"
        f"cycles: {cycles_spent}/{max_cycles}\n\n"
        f"{constraints_block}\n\n"
        "Verdict guidance: needs_revision feedback must name the next "
        "structural step (expand X / prune Y / paradigm shift / merge / "
        "finalize). Never accept prose claims of improvement — only "
        "meta.test_trunk_score counts. Selecting on the held-out test split "
        "outside merge_experiment is a blocked outcome."
    )
    if convergence:
        block += "\n\n" + convergence
    return block
