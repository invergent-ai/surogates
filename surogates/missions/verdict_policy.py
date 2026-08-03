"""Deterministic gates on the mission judge's verdict.

`arbor/evaluator_policy.py` already establishes the principle for research
missions: the LLM does not mint the terminal verdict. ``satisfied`` is demoted
unless a machine-written score improved and the report task is done.

Standard missions had no equivalent — the judge's word was final, so a
coordinator that talked its way to "done" was done.

The corroborating signal is deliberately machine-written. Task status is set
by the dispatcher and the completion classifier, never by the coordinator's
prose. A ``tool.result`` event would not do: it proves *some* tool ran, and
the coordinator chooses the tool and its arguments, so
``bash -c 'echo tests passed'` produces one.

Nothing here executes anything. Per-criterion validator commands — declared
at mission creation with trusted argv and exit-code-only semantics, the way
arbor's ``eval_cmd_test`` works — are a separate and much larger design
(sandbox access, timeouts, argument trust) and are deliberately not this.
"""

from __future__ import annotations

from typing import Any

__all__ = ["adjust_mission_verdict"]

_DEMOTION_FEEDBACK = (
    "A mission is not satisfied while none of its tasks completed. Either "
    "finish the outstanding work, or — if the objective genuinely needs no "
    "further tasks — say so explicitly and explain what evidence supports "
    "that."
)


def adjust_mission_verdict(
    verdict: dict[str, Any],
    *,
    completed_tasks: int,
    total_tasks: int | None = None,
) -> dict[str, Any]:
    """Demote a ``satisfied`` verdict that nothing corroborates.

    *completed_tasks* counts mission tasks in a done state; *total_tasks*
    counts every mission task. When a mission spawned no tasks at all there
    is nothing to corroborate against, so the gate stands aside rather than
    deadlocking a mission that was never meant to decompose.

    Verdicts other than ``satisfied`` pass through: they already claim less
    than the evidence would need to support.
    """
    if not isinstance(verdict, dict) or verdict.get("result") != "satisfied":
        return verdict
    if completed_tasks > 0:
        return verdict
    if not total_tasks:
        return verdict
    return {
        "result": "needs_revision",
        "explanation": (
            "satisfied rejected: no mission task completed, so nothing "
            "corroborates the claim"
        ),
        "feedback": _DEMOTION_FEEDBACK,
    }
