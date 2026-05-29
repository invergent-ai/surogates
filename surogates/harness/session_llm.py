"""Per-session LLM client bundle.

Plan 2 / Task 5.  Replaces the process-wide ``AsyncOpenAI`` instance
(plus the three auxiliary-client builders) the harness used in helm
mode.  Each session gets its own bundle constructed from the four
:class:`~surogates.runtime.LLMEndpoint` slots on
:class:`~surogates.runtime.AgentRuntimeContext`; the bundle is
immutable so the harness cannot mid-turn-swap a client and silently
route a continuation through a different LLM than the turn started
on.

``aclose()`` releases every wrapped client's connection pool — the
dispatcher calls it at session end so a long-running worker process
does not accumulate one pool per processed session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["ResolvedLLM", "SessionLLMClients"]


@dataclass(frozen=True, slots=True)
class ResolvedLLM:
    """A fully-resolved LLM slot: client + the model string the harness
    passes on every call.  ``client`` is an ``AsyncOpenAI``
    (or a duck-typed equivalent — the harness only calls
    ``chat.completions.create`` and ``close``)."""

    client: Any
    model: str


@dataclass(frozen=True, slots=True)
class SessionLLMClients:
    """Bundle of the four LLM slots for one session.

    ``main`` is always present (every session needs a main LLM).
    ``summary`` / ``vision`` / ``advisor`` are optional — agents that
    don't configure them get ``None`` and the harness skips the
    corresponding code path (no fallback to ``main`` for the
    auxiliary slots — Plan 1 + 6 governance keep the four slots
    distinct).
    """

    main: ResolvedLLM
    summary: ResolvedLLM | None
    vision: ResolvedLLM | None
    advisor: ResolvedLLM | None

    async def aclose(self) -> None:
        """Close every wrapped client's connection pool.

        Called by the dispatcher at session end.  Skips ``None`` slots
        silently so cheaper agents that don't configure summary /
        vision / advisor still work."""
        for slot in (self.main, self.summary, self.vision, self.advisor):
            if slot is None:
                continue
            await slot.client.close()
