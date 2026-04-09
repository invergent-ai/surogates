"""Thread-safe iteration budget shared across parent and subagent sessions.

Each agent (parent or subagent) gets its own ``IterationBudget``.
The parent's budget is capped at ``max_total`` (default 90).
Each subagent gets an independent budget capped at
``delegation.max_total`` (default 90) — this means total
iterations across parent + subagents can exceed the parent's cap.

``execute_code`` (programmatic tool calling) iterations are refunded via
:meth:`refund` so they don't eat into the budget.
"""

from __future__ import annotations

import threading


class IterationBudget:
    """Thread-safe iteration counter."""

    def __init__(self, max_total: int = 90) -> None:
        self.max_total = max_total
        self._used = 0
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """Try to consume one iteration.  Returns True if allowed."""
        with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    def refund(self) -> None:
        """Give back one iteration (e.g. for execute_code turns)."""
        with self._lock:
            if self._used > 0:
                self._used -= 1

    @property
    def used(self) -> int:
        return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_total - self._used)

    @property
    def exhausted(self) -> bool:
        """``True`` when no iterations remain."""
        with self._lock:
            return self._used >= self.max_total

    def __repr__(self) -> str:
        with self._lock:
            return (
                f"IterationBudget(used={self._used}, "
                f"max_total={self.max_total})"
            )
