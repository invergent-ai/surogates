"""Deterministic gates on the mission judge's verdict.

The LLM does not mint the terminal verdict: without this, the judge's word
was final, so a coordinator that talked its way to "done" was done.

The corroborating signal is deliberately machine-written. An agent's status
is set by the harness as its session runs and ends, never by the
coordinator's prose. A ``tool.result`` event would not do: it proves *some* tool ran, and
the coordinator chooses the tool and its arguments, so
``bash -c 'echo tests passed'` produces one.

Nothing here executes anything. Per-criterion validator commands — declared
at mission creation with trusted argv and exit-code-only semantics — are a
separate and much larger design (sandbox access, timeouts, argument trust)
and are deliberately not this.
"""

from __future__ import annotations

from typing import Any

__all__ = ["adjust_mission_verdict"]

_DEMOTION_FEEDBACK = (
    "A mission is not satisfied while none of its agents completed. Either "
    "finish the outstanding work, or — if the objective genuinely needs no "
    "further agents — say so explicitly and explain what evidence supports "
    "that."
)


def adjust_mission_verdict(
    verdict: dict[str, Any],
    *,
    completed_agents: int,
    total_agents: int | None = None,
) -> dict[str, Any]:
    """Demote a ``satisfied`` verdict that nothing corroborates.

    *completed_agents* counts the mission's agents that completed;
    *total_agents* counts all of them. When a mission started no agents at
    all there is nothing to corroborate against, so the gate stands aside
    rather than deadlocking a mission that was never meant to decompose.

    Verdicts other than ``satisfied`` pass through: they already claim less
    than the evidence would need to support.
    """
    if not isinstance(verdict, dict) or verdict.get("result") != "satisfied":
        return verdict
    if completed_agents > 0:
        return verdict
    if not total_agents:
        return verdict
    return {
        "result": "needs_revision",
        "explanation": (
            "satisfied rejected: no mission agent completed, so nothing "
            "corroborates the claim"
        ),
        "feedback": _DEMOTION_FEEDBACK,
    }
