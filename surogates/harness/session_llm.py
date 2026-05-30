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

import logging
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from surogates.runtime.context import AgentRuntimeContext, LLMEndpoint

__all__ = [
    "ResolvedLLM",
    "SessionLLMClients",
    "_close_partial_bundle",
    "build_session_llm_clients",
]


logger = logging.getLogger(__name__)


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


async def _close_partial_bundle(resolved: list[ResolvedLLM]) -> None:
    """Best-effort aclose every already-resolved slot on a failed build.

    Plan 2 post-review: ``build_session_llm_clients`` (and the helm
    adapter ``_build_helm_session_llm_clients`` in
    :mod:`surogates.orchestrator.worker`) instantiate
    ``AsyncOpenAI`` per slot.  If a later slot raises, the
    already-resolved earlier slots would leak their connection pools
    over the worker process's lifetime — a flaky vault produces an
    unbounded FD leak.  Both factories drain through this helper on
    failure before re-raising the original exception.
    """
    for slot in resolved:
        try:
            await slot.client.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            logger.warning(
                "Failed to aclose partial LLM client during bundle "
                "build cleanup; the original error is being re-raised",
                exc_info=True,
            )


async def build_session_llm_clients(
    ctx: AgentRuntimeContext,
    *,
    vault: Any,
    user_id: Any = None,
) -> SessionLLMClients:
    """Build the four-slot LLM bundle for one session.

    Plan 2 / Task 6.  Each ``LLMEndpoint`` on the context becomes a
    ``ResolvedLLM`` — one ``AsyncOpenAI`` instance pointed at
    ``endpoint.base_url`` with the vault-resolved API key, paired
    with ``endpoint.model``.

    The four slots are independent connection pools; mid-call
    failover (Plan 6) lives in the harness, not here.  Plan 2's
    contract is just "give me clients keyed to the four slots; I'll
    call them how the agent wants."

    Partial-build failure is handled via :func:`_close_partial_bundle`:
    if any slot's vault resolution or client construction raises after
    earlier slots have been instantiated, the earlier slots are
    aclose()d before the exception propagates so a flaky vault cannot
    leak ``AsyncOpenAI`` instances unboundedly.
    """
    if ctx.llm_main is None:
        raise ValueError(
            f"agent {ctx.agent_id} has no llm_main configured — "
            "every session needs a main LLM",
        )

    resolved: list[ResolvedLLM] = []

    async def _resolve(endpoint: LLMEndpoint) -> ResolvedLLM:
        key = await vault.resolve_ref(
            endpoint.api_key_ref, org_id=ctx.org_id, user_id=user_id,
        )
        client = AsyncOpenAI(api_key=key, base_url=endpoint.base_url)
        slot = ResolvedLLM(client=client, model=endpoint.model)
        resolved.append(slot)
        return slot

    async def _opt(endpoint: LLMEndpoint | None) -> ResolvedLLM | None:
        if endpoint is None:
            return None
        return await _resolve(endpoint)

    try:
        main = await _resolve(ctx.llm_main)
        summary = await _opt(ctx.llm_summary)
        vision = await _opt(ctx.llm_vision)
        advisor = await _opt(ctx.llm_advisor)
    except BaseException:
        await _close_partial_bundle(resolved)
        raise

    return SessionLLMClients(
        main=main,
        summary=summary,
        vision=vision,
        advisor=advisor,
    )
