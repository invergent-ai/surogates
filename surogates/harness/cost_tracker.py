"""Per-session cost accumulation.

Tracks total input/output tokens and estimated cost across all LLM calls
in a session.  Updated after each LLM response.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SessionCostTracker:
    """Accumulates cost data across LLM calls within a session."""

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_reasoning_tokens: int = 0
    total_cost_usd: float = 0.0
    call_count: int = 0

    def record_call(
        self,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        *,
        cache_read_tokens: int = 0,
        reasoning_tokens: int = 0,
    ) -> None:
        """Record a single LLM call's token usage and cost."""
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_cache_read_tokens += cache_read_tokens
        self.total_reasoning_tokens += reasoning_tokens
        self.total_cost_usd += cost_usd
        self.call_count += 1

    def summary(self) -> dict:
        """Return a dict summarising all accumulated cost data."""
        return {
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cache_read_tokens": self.total_cache_read_tokens,
            "total_reasoning_tokens": self.total_reasoning_tokens,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "call_count": self.call_count,
        }
