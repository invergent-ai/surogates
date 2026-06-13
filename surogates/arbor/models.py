"""Pydantic shapes shared by the arbor tools, harvest hook, and evaluator."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

NodeStatus = Literal["pending", "running", "done", "failed", "merged", "pruned"]

#: Statuses a node can never move out of (mutation guard in the store).
TERMINAL_NODE_STATUSES: tuple[str, ...] = ("merged", "pruned")

#: Statuses that consume one unit of the cycle budget. A failed/timed-out
#: experiment spends budget exactly like a successful one — Arbor's
#: "failed runs spend budget" rule.
BUDGET_SPENDING_STATUSES: tuple[str, ...] = ("done", "failed", "merged", "pruned")


class ExperimentReport(BaseModel):
    """Structured extraction target for an executor's free-text report.

    Used by the harvest hook only when the worker forgot to put the
    fields in ``worker_complete(metadata=...)``.
    """

    node_key: str = ""
    score: float | None = None
    insight: str = ""
    result: str = ""
    branch: str = ""
