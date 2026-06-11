"""Per-session LLM client bundle.

Each session gets its own bundle constructed from the
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
    "ResolvedVideoEndpoint",
    "SessionLLMClients",
    "_close_partial_bundle",
    "build_session_llm_clients",
    "resolve_video_endpoint",
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
    """Bundle of the per-session LLM slots.

    ``main`` is always present (every session needs a main LLM).
    ``summary`` / ``vision`` / ``advisor`` / ``image`` are optional —
    agents that don't configure them get ``None`` and the harness skips
    the corresponding code path (no fallback to ``main`` for the
    auxiliary slots).
    """

    main: ResolvedLLM
    summary: ResolvedLLM | None
    vision: ResolvedLLM | None
    advisor: ResolvedLLM | None
    image: ResolvedLLM | None = None

    async def aclose(self) -> None:
        """Close every wrapped client's connection pool.

        Called by the dispatcher at session end.  Skips ``None`` slots
        silently so cheaper agents that don't configure the optional
        slots still work."""
        for slot in (
            self.main, self.summary, self.vision, self.advisor, self.image,
        ):
            if slot is None:
                continue
            await slot.client.close()


async def _close_partial_bundle(resolved: list[ResolvedLLM]) -> None:
    """Best-effort aclose every already-resolved slot on a failed build.

    ``build_session_llm_clients`` instantiates ``AsyncOpenAI`` per
    slot.  If a later slot raises, the already-resolved earlier
    slots would leak their connection pools over the worker
    process's lifetime — a flaky vault produces an unbounded FD
    leak.  The factory drains through this helper on failure
    before re-raising the original exception.
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
    settings: Any = None,
) -> SessionLLMClients:
    """Build the per-session LLM bundle.

    Each ``LLMEndpoint`` on the context becomes a
    ``ResolvedLLM`` — one ``AsyncOpenAI`` instance pointed at
    ``endpoint.base_url`` with the vault-resolved API key, paired
    with ``endpoint.model``.

    The slots are independent connection pools; mid-call
    failover lives in the harness, not here.  The
    contract is just "give me clients keyed to the slots; I'll
    call them how the agent wants."

    The ``image`` slot additionally falls back to the operator-level
    ``settings.llm.image_*`` values (raw keys, no vault) when the
    context has no ``llm_image`` endpoint — media generation is
    deployment-configured until the management plane sends per-agent
    endpoints.

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
        # User-principal sessions fall back to the org-scoped credential
        # when the user has no personal override.  Platform admission and
        # BYO model keys are seeded org-scoped (``user_id IS NULL``), so a
        # user-scoped lookup misses them; without this fallback the client
        # is built with no key and ``AsyncOpenAI`` raises 'Missing
        # credentials', failing every user-principal (webapp) session
        # while service-account sessions keep working.  Mirrors the
        # user->org fallback in ``mcp_proxy.loader``.
        if key is None and user_id is not None:
            key = await vault.resolve_ref(
                endpoint.api_key_ref, org_id=ctx.org_id, user_id=None,
            )
        client = AsyncOpenAI(api_key=key, base_url=endpoint.base_url)
        slot = ResolvedLLM(client=client, model=endpoint.model)
        resolved.append(slot)
        return slot

    async def _opt(endpoint: LLMEndpoint | None) -> ResolvedLLM | None:
        if endpoint is None:
            return None
        return await _resolve(endpoint)

    def _settings_image() -> ResolvedLLM | None:
        """Settings-based image slot fallback (raw key, no vault).

        Mirrors ``auxiliary_client.build_summary_auxiliary_llm``: config
        carries plaintext keys, so vault resolution does not apply.  The
        client joins ``resolved`` so a later slot failure still closes it.
        """
        if settings is None:
            return None
        llm = settings.llm
        if not llm.image_model:
            return None
        client_kwargs: dict[str, Any] = {
            "api_key": llm.image_api_key or llm.api_key or "EMPTY",
        }
        base_url = llm.image_base_url or llm.base_url
        if base_url:
            client_kwargs["base_url"] = base_url
        slot = ResolvedLLM(client=AsyncOpenAI(**client_kwargs), model=llm.image_model)
        resolved.append(slot)
        return slot

    try:
        main = await _resolve(ctx.llm_main)
        summary = await _opt(ctx.llm_summary)
        vision = await _opt(ctx.llm_vision)
        advisor = await _opt(ctx.llm_advisor)
        image = await _opt(ctx.llm_image)
        if image is None:
            image = _settings_image()
    except BaseException:
        await _close_partial_bundle(resolved)
        raise

    return SessionLLMClients(
        main=main,
        summary=summary,
        vision=vision,
        advisor=advisor,
        image=image,
    )


@dataclass(frozen=True, slots=True)
class ResolvedVideoEndpoint:
    """Resolved video-generation endpoint for one session.

    Holds the plaintext key because the video tool calls the provider's
    ``/videos`` job API over raw httpx — there is no SDK client object
    to hide the key inside, unlike the chat-completions slots.
    """

    model: str
    base_url: str
    api_key: str


async def resolve_video_endpoint(
    ctx: AgentRuntimeContext,
    *,
    vault: Any,
    user_id: Any = None,
    settings: Any = None,
) -> ResolvedVideoEndpoint | None:
    """Resolve the per-session video endpoint: context slot, then settings.

    The context slot carries a vault ref (resolved here, with the same
    user→org credential fallback as the chat slots); the settings path
    carries raw keys.  Returns ``None`` when neither configures a model —
    the generate_video tool then reports itself unavailable.
    """
    endpoint = ctx.llm_video
    if endpoint is not None and endpoint.model:
        key = await vault.resolve_ref(
            endpoint.api_key_ref, org_id=ctx.org_id, user_id=user_id,
        )
        if key is None and user_id is not None:
            key = await vault.resolve_ref(
                endpoint.api_key_ref, org_id=ctx.org_id, user_id=None,
            )
        return ResolvedVideoEndpoint(
            model=endpoint.model,
            base_url=endpoint.base_url,
            api_key=key or "",
        )
    if settings is None:
        return None
    llm = settings.llm
    if not llm.video_model:
        return None
    return ResolvedVideoEndpoint(
        model=llm.video_model,
        base_url=llm.video_base_url or llm.base_url,
        api_key=llm.video_api_key or llm.api_key,
    )
