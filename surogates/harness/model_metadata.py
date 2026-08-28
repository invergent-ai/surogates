"""Model catalog with context-window sizes, capability flags, and pricing.

Provides :data:`MODEL_CATALOG` for fast lookups and convenience functions
for token estimation and cost calculation.

Context-probing helpers:

- :data:`CONTEXT_PROBE_TIERS` -- descending tiers for iterative step-down.
- :func:`get_next_probe_tier` -- returns the next lower tier.
- :func:`parse_context_limit_from_error` -- extracts the actual limit from
  an API error message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """Static metadata for a single LLM model."""

    id: str
    context_window: int
    max_output_tokens: int
    input_cost_per_1k: float
    output_cost_per_1k: float
    supports_tools: bool = True
    supports_vision: bool = False
    supports_streaming: bool = True


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

# Keys are OpenRouter model ids -- the reference identity for every model we
# price.  OpenRouter publishes context window, max output, per-token pricing
# and modality for all of them in one place, so these numbers can be
# re-derived from ``GET https://openrouter.ai/api/v1/models`` instead of being
# hand-maintained per provider.  HuggingFace repo ids, gateway (yunwu) ids,
# dated release slugs and bare short names are all ALIASES onto these keys.
MODEL_CATALOG: dict[str, ModelInfo] = {
    # --- OpenAI ------------------------------------------------------------
    # The 5.6 line is three price/capability points on one 1.05M window;
    # OpenRouter also carries ``-pro`` and ``:batch`` variants of each, which
    # we do not route to.
    "openai/gpt-5.6-luna": ModelInfo(
        id="openai/gpt-5.6-luna",
        context_window=1_050_000,
        max_output_tokens=128_000,
        input_cost_per_1k=0.0001,
        output_cost_per_1k=0.0006,
        supports_vision=True,
    ),
    "openai/gpt-5.6-terra": ModelInfo(
        id="openai/gpt-5.6-terra",
        context_window=1_050_000,
        max_output_tokens=128_000,
        input_cost_per_1k=0.001,
        output_cost_per_1k=0.006,
        supports_vision=True,
    ),
    "openai/gpt-5.6-sol": ModelInfo(
        id="openai/gpt-5.6-sol",
        context_window=1_050_000,
        max_output_tokens=128_000,
        input_cost_per_1k=0.005,
        output_cost_per_1k=0.03,
        supports_vision=True,
    ),
    # --- Anthropic ---------------------------------------------------------
    "anthropic/claude-sonnet-5": ModelInfo(
        id="anthropic/claude-sonnet-5",
        context_window=1_000_000,
        max_output_tokens=128_000,
        input_cost_per_1k=0.002,
        output_cost_per_1k=0.01,
        supports_vision=True,
    ),
    "anthropic/claude-opus-5": ModelInfo(
        id="anthropic/claude-opus-5",
        context_window=1_000_000,
        max_output_tokens=128_000,
        input_cost_per_1k=0.005,
        output_cost_per_1k=0.025,
        supports_vision=True,
    ),
    # --- DeepSeek ----------------------------------------------------------
    "deepseek/deepseek-v4-pro": ModelInfo(
        id="deepseek/deepseek-v4-pro",
        context_window=1_048_576,
        max_output_tokens=393_216,
        input_cost_per_1k=0.00063168,
        output_cost_per_1k=0.00126336,
    ),
    # The dated -0731 build is the current Flash release; the undated
    # ``deepseek/deepseek-v4-flash`` is the older 0423 one and is kept only
    # as an alias, so callers naming either get the current rates.
    "deepseek/deepseek-v4-flash-0731": ModelInfo(
        id="deepseek/deepseek-v4-flash-0731",
        context_window=1_048_576,
        max_output_tokens=384_000,
        input_cost_per_1k=8e-05,
        output_cost_per_1k=0.00018,
    ),
    # --- Google ------------------------------------------------------------
    "google/gemini-3-flash-preview": ModelInfo(
        id="google/gemini-3-flash-preview",
        context_window=1_048_576,
        max_output_tokens=65_536,
        input_cost_per_1k=0.0005,
        output_cost_per_1k=0.003,
        supports_vision=True,
    ),
    "google/gemini-3.5-flash": ModelInfo(
        id="google/gemini-3.5-flash",
        context_window=1_048_576,
        max_output_tokens=65_536,
        input_cost_per_1k=0.0015,
        output_cost_per_1k=0.009,
        supports_vision=True,
    ),
    "google/gemini-3.7-flash": ModelInfo(
        id="google/gemini-3.7-flash",
        context_window=1_048_576,
        max_output_tokens=65_536,
        input_cost_per_1k=0.000375,
        output_cost_per_1k=0.001875,
        supports_vision=True,
    ),
    # --- Z.AI --------------------------------------------------------------
    # 5.3 is text-only; the Flash sibling is the multimodal one (image+video).
    "z-ai/glm-5.3": ModelInfo(
        id="z-ai/glm-5.3",
        context_window=1_048_576,
        max_output_tokens=131_072,
        input_cost_per_1k=0.0014,
        output_cost_per_1k=0.0044,
    ),
    # OpenRouter advertises a 1.31M window for the model but the serving
    # provider caps at 1.05M, so we size to what is actually served.
    "z-ai/glm-5.3-flash": ModelInfo(
        id="z-ai/glm-5.3-flash",
        context_window=1_048_576,
        max_output_tokens=131_072,
        input_cost_per_1k=7.5e-05,
        output_cost_per_1k=0.00025,
        supports_vision=True,
    ),
    "z-ai/glm-5.2": ModelInfo(
        id="z-ai/glm-5.2",
        context_window=1_048_576,
        max_output_tokens=262_144,
        input_cost_per_1k=0.00076,
        output_cost_per_1k=0.00242,
    ),
    # --- Meta ---------------------------------------------------------------
    # Output cap is a local default: OpenRouter publishes no
    # ``max_completion_tokens`` for this model (see the Moonshot note below).
    "meta/muse-spark-1.2": ModelInfo(
        id="meta/muse-spark-1.2",
        context_window=1_048_576,
        max_output_tokens=131_072,
        input_cost_per_1k=0.00125,
        output_cost_per_1k=0.00425,
        supports_vision=True,
    ),
    # Note the much smaller 128k window -- the only model here that is not
    # 1M-class, so context sizing differs materially from its siblings.
    "meta/muse-glimmer-30b": ModelInfo(
        id="meta/muse-glimmer-30b",
        context_window=131_072,
        max_output_tokens=32_768,
        input_cost_per_1k=0.00035,
        output_cost_per_1k=0.0015,
        supports_vision=True,
    ),
    # --- Moonshot ----------------------------------------------------------
    # OpenRouter publishes no ``max_completion_tokens`` for this model, so the
    # output cap below is a conservative local default rather than an upstream
    # figure -- it bounds what we request, so erring low costs a continuation
    # at worst, while a fabricated high value would get rejected at the API.
    "moonshotai/kimi-k3": ModelInfo(
        id="moonshotai/kimi-k3",
        context_window=1_048_576,
        max_output_tokens=131_072,
        input_cost_per_1k=0.003,
        output_cost_per_1k=0.015,
        supports_vision=True,
    ),
    # --- Thinking Machines --------------------------------------------------
    # ``supports_vision`` is the only modality flag :class:`ModelInfo` has, so
    # the audio input this model accepts (and the audio+video mimo-v2.5 below
    # accepts) is not represented.  Noted rather than modelled because nothing
    # routes either yet; add a flag when something does.
    "thinkingmachines/inkling-small": ModelInfo(
        id="thinkingmachines/inkling-small",
        context_window=524_288,
        max_output_tokens=262_144,
        input_cost_per_1k=0.00045,
        output_cost_per_1k=0.0012,
        supports_vision=True,
    ),
    # --- Alibaba -----------------------------------------------------------
    "qwen/qwen3.8-max": ModelInfo(
        id="qwen/qwen3.8-max",
        context_window=1_000_000,
        max_output_tokens=131_072,
        input_cost_per_1k=0.002,
        output_cost_per_1k=0.006,
        supports_vision=True,
    ),
    # Same price point as Max but text-only, with a 262k output cap.
    "qwen/qwen3.8-2.4t-a95b": ModelInfo(
        id="qwen/qwen3.8-2.4t-a95b",
        context_window=1_000_000,
        max_output_tokens=262_144,
        input_cost_per_1k=0.002,
        output_cost_per_1k=0.006,
    ),
    "qwen/qwen3.8-flash": ModelInfo(
        id="qwen/qwen3.8-flash",
        context_window=1_000_000,
        max_output_tokens=131_072,
        input_cost_per_1k=0.00015,
        output_cost_per_1k=0.00047,
        supports_vision=True,
    ),
    # --- Tencent ------------------------------------------------------------
    "tencent/hy3": ModelInfo(
        id="tencent/hy3",
        context_window=262_144,
        max_output_tokens=128_000,
        input_cost_per_1k=0.000132,
        output_cost_per_1k=0.000528,
    ),
    # --- Xiaomi -------------------------------------------------------------
    "xiaomi/mimo-v2.5": ModelInfo(
        id="xiaomi/mimo-v2.5",
        context_window=1_050_000,
        max_output_tokens=131_072,
        input_cost_per_1k=0.00014,
        output_cost_per_1k=0.00028,
        supports_vision=True,
    ),
    # --- Poolside -----------------------------------------------------------
    # The paid tier deliberately, not the ``:free`` one: free variants serve a
    # quarter of the context (262k vs 1M) and a quarter of the output cap, and
    # a zero rate is indistinguishable from an unpriced sentinel to
    # ``_has_rate``, so usage would record as a pricing gap rather than a cost.
    "poolside/laguna-s-2.1": ModelInfo(
        id="poolside/laguna-s-2.1",
        context_window=1_048_576,
        max_output_tokens=131_072,
        input_cost_per_1k=9e-05,
        output_cost_per_1k=0.00018,
    ),
    # --- Surogate tier sentinels ------------------------------------------
    # Sessions run under a tier sentinel rather than a concrete model id.
    # They are in the catalog so the compressor can size their context, and
    # they deliberately carry NO rate: ``estimate_call_cost`` falls through
    # to the model the gateway reports as having actually served the call.
    # Pricing them here would bill every session at the wrong rate.
    "surogate": ModelInfo(
        id="surogate",
        context_window=262_144,
        max_output_tokens=32_768,
        input_cost_per_1k=0.0,
        output_cost_per_1k=0.0,
        supports_vision=True,
    ),
    "surogate-pro": ModelInfo(
        id="surogate-pro",
        context_window=262_144,
        max_output_tokens=32_768,
        input_cost_per_1k=0.0,
        output_cost_per_1k=0.0,
        supports_vision=True,
    ),
}

# Alias -> canonical OpenRouter id.
#
# Four shapes: the bare model name, the dated release slug OpenRouter reports
# as ``canonical_slug``, the HuggingFace repo id for open-weight models, and
# the gateway/preset ids our own config uses (``claude-sonnet-5``,
# ``@preset/...``).  Every value MUST be a key of MODEL_CATALOG -- an alias
# pointing at a missing entry resolves to ``None`` and the caller silently
# falls back to a default context window and zero pricing, with no error
# anywhere.  ``test_model_metadata`` pins the invariant so retiring a model
# can never leave its aliases behind pointing at nothing.
_ALIASES: dict[str, str] = {
    "gpt-5.6-luna": "openai/gpt-5.6-luna",
    "openai/gpt-5.6-luna-20260709": "openai/gpt-5.6-luna",
    "gpt-5.6-terra": "openai/gpt-5.6-terra",
    "openai/gpt-5.6-terra-20260709": "openai/gpt-5.6-terra",
    "gpt-5.6-sol": "openai/gpt-5.6-sol",
    "openai/gpt-5.6-sol-20260709": "openai/gpt-5.6-sol",
    "claude-sonnet-5": "anthropic/claude-sonnet-5",
    "anthropic/claude-sonnet-5-20260630": "anthropic/claude-sonnet-5",
    "claude-opus-5": "anthropic/claude-opus-5",
    "anthropic/claude-opus-5-20260723": "anthropic/claude-opus-5",
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "deepseek/deepseek-v4-pro-20260423": "deepseek/deepseek-v4-pro",
    "deepseek-ai/DeepSeek-V4-Pro": "deepseek/deepseek-v4-pro",
    "deepseek-v4-flash-0731": "deepseek/deepseek-v4-flash-0731",
    "deepseek/deepseek-v4-flash-20260731": "deepseek/deepseek-v4-flash-0731",
    "deepseek-ai/DeepSeek-V4-Flash-0731": "deepseek/deepseek-v4-flash-0731",
    # Undated Flash ids point at the current release rather than the older
    # 0423 build they originally named.
    "deepseek/deepseek-v4-flash": "deepseek/deepseek-v4-flash-0731",
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash-0731",
    "deepseek/deepseek-v4-flash-20260423": "deepseek/deepseek-v4-flash-0731",
    "deepseek-ai/DeepSeek-V4-Flash": "deepseek/deepseek-v4-flash-0731",
    "gemini-3-flash-preview": "google/gemini-3-flash-preview",
    "google/gemini-3-flash-preview-20251217": "google/gemini-3-flash-preview",
    "gemini-3.5-flash": "google/gemini-3.5-flash",
    "google/gemini-3.5-flash-20260519": "google/gemini-3.5-flash",
    "gemini-3.7-flash": "google/gemini-3.7-flash",
    "google/gemini-3.7-flash-20260813": "google/gemini-3.7-flash",
    "glm-5.3": "z-ai/glm-5.3",
    "z-ai/glm-5.3-20260816": "z-ai/glm-5.3",
    "zai-org/GLM-5.3": "z-ai/glm-5.3",
    "glm-5.3-flash": "z-ai/glm-5.3-flash",
    "z-ai/glm-5.3-flash-20260826": "z-ai/glm-5.3-flash",
    "zai-org/GLM-5.3-Flash": "z-ai/glm-5.3-flash",
    "glm-5.2": "z-ai/glm-5.2",
    "z-ai/glm-5.2-20260616": "z-ai/glm-5.2",
    "zai-org/GLM-5.2": "z-ai/glm-5.2",
    "laguna-s-2.1": "poolside/laguna-s-2.1",
    "poolside/laguna-s-2.1-20260720": "poolside/laguna-s-2.1",
    "poolside/Laguna-S-2.1": "poolside/laguna-s-2.1",
    "hy3": "tencent/hy3",
    "tencent/hy3-20260706": "tencent/hy3",
    "tencent/Hy3": "tencent/hy3",
    "mimo-v2.5": "xiaomi/mimo-v2.5",
    "xiaomi/mimo-v2.5-20260422": "xiaomi/mimo-v2.5",
    "XiaomiMiMo/MiMo-V2.5": "xiaomi/mimo-v2.5",
    "inkling-small": "thinkingmachines/inkling-small",
    "thinkingmachines/inkling-small-20260730": "thinkingmachines/inkling-small",
    "thinkingmachines/Inkling-Small": "thinkingmachines/inkling-small",
    "muse-glimmer-30b": "meta/muse-glimmer-30b",
    "meta/muse-glimmer-30b-20260810": "meta/muse-glimmer-30b",
    "meta-models/Muse-Glimmer-30B": "meta/muse-glimmer-30b",
    "muse-spark-1.2": "meta/muse-spark-1.2",
    "meta/muse-spark-1.2-20260805": "meta/muse-spark-1.2",
    "kimi-k3": "moonshotai/kimi-k3",
    "moonshotai/kimi-k3-20260715": "moonshotai/kimi-k3",
    "moonshotai/Kimi-K3": "moonshotai/kimi-k3",
    "qwen3.8-max": "qwen/qwen3.8-max",
    "qwen/qwen3.8-max-20260803": "qwen/qwen3.8-max",
    "qwen3.8-2.4t-a95b": "qwen/qwen3.8-2.4t-a95b",
    "qwen/qwen3.8-2.4t-a95b-20260812": "qwen/qwen3.8-2.4t-a95b",
    "Qwen/Qwen3.8-2.4T-A95B": "qwen/qwen3.8-2.4t-a95b",
    "qwen3.8-flash": "qwen/qwen3.8-flash",
    "qwen/qwen3.8-flash-20260826": "qwen/qwen3.8-flash",
    # Short names and gateway ids used by our own config and by callers.
    "sonnet": "anthropic/claude-sonnet-5",
    "claude-sonnet": "anthropic/claude-sonnet-5",
    "opus": "anthropic/claude-opus-5",
    "claude-opus": "anthropic/claude-opus-5",
    "deepseek": "deepseek/deepseek-v4-pro",
    "deepseek-chat": "deepseek/deepseek-v4-pro",
    "deepseek-reasoner": "deepseek/deepseek-v4-flash-0731",
    "gemini-3-flash": "google/gemini-3-flash-preview",
    "@preset/glm-5-2": "z-ai/glm-5.2",
    "@preset/qwen-3-8-max": "qwen/qwen3.8-max",
}


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def get_model_info(model_id: str) -> ModelInfo | None:
    """Look up model metadata by exact ID or known alias.

    Returns ``None`` if the model is not in the catalog.
    """
    info = MODEL_CATALOG.get(model_id)
    if info is not None:
        return info
    canonical = _ALIASES.get(model_id)
    if canonical is not None:
        return MODEL_CATALOG.get(canonical)
    return None


def resolve_model_info(
    model_id: str,
    *,
    base_url: str = "",
    api_key: str = "",
    overrides: dict[str, dict] | None = None,
) -> ModelInfo | None:
    """Resolve :class:`ModelInfo` using the full fallback chain.

    Precedence (first match wins):

    1. ``overrides`` — a ``{model_id: {context_window, max_output_tokens}}``
       map sourced from operator config.  Applied on top of whatever the
       catalog or provider reports, so operators can correct a single
       field (e.g., context window) without re-stating pricing.
    2. Static catalog (:data:`MODEL_CATALOG`) + its aliases.  Human-curated
       pricing and capability flags.
    3. Provider discovery via :mod:`surogates.harness.model_discovery` —
       a one-shot lazy fetch of ``{base_url}/models`` that covers every
       model the provider routes (OpenRouter, LM Studio, vLLM, etc.).

    Returns ``None`` when every source comes up empty.  Callers are
    expected to warn and fall back to a safe default; we don't fabricate
    a :class:`ModelInfo` here because doing so hides the configuration
    gap from the operator.
    """
    from dataclasses import replace

    override = (overrides or {}).get(model_id)
    base: ModelInfo | None = None

    # 2. Static catalog first — it's the authoritative source for any
    # model it knows about.  We still apply override fields (if any) on
    # top, so operators can tweak without replacing the whole record.
    base = get_model_info(model_id)

    # 3. Provider discovery — only consulted when the static catalog
    # misses.  Importing at call time keeps ``model_metadata`` usable
    # without a transitive httpx dependency (tests / minimal runtimes).
    if base is None and base_url:
        from surogates.harness.model_discovery import discover_model
        base = discover_model(model_id, base_url=base_url, api_key=api_key)

    # 1. Apply override last so it wins over both sources above.
    if override:
        if base is None:
            # Build a skeleton the override can layer onto.  Pricing
            # defaults to 0.0 and capability flags default to the
            # safest assumption (no tools / vision) — callers can
            # override those explicitly if they care.
            base = ModelInfo(
                id=model_id,
                context_window=0,
                max_output_tokens=0,
                input_cost_per_1k=0.0,
                output_cost_per_1k=0.0,
                supports_tools=True,
                supports_vision=False,
                supports_streaming=True,
            )
        updates: dict = {}
        if "context_window" in override:
            updates["context_window"] = int(override["context_window"])
        if "max_output_tokens" in override:
            updates["max_output_tokens"] = int(override["max_output_tokens"])
        if "input_cost_per_1k" in override:
            updates["input_cost_per_1k"] = float(override["input_cost_per_1k"])
        if "output_cost_per_1k" in override:
            updates["output_cost_per_1k"] = float(override["output_cost_per_1k"])
        if updates:
            base = replace(base, **updates)

    return base


# ---------------------------------------------------------------------------
# Estimation helpers
# ---------------------------------------------------------------------------

# Rough heuristic: ~4 characters per token for English text.
_CHARS_PER_TOKEN: float = 4.0


def estimate_tokens(text: str) -> int:
    """Estimate the number of tokens in *text*.

    Uses the widely-accepted heuristic of approximately 4 characters per
    token for English prose.  This is intentionally conservative (over-
    counting) so callers do not accidentally exceed context windows.
    """
    if not text:
        return 0
    return max(1, int(len(text) / _CHARS_PER_TOKEN + 0.5))


# Cached-input reads are billed at a fraction of the normal input rate
# (Anthropic / Bedrock ephemeral cache reads are ~0.1x the input price).
_CACHE_READ_DISCOUNT: float = 0.1


def estimate_cost(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
) -> float:
    """Estimate the USD cost for a single LLM call.

    ``cache_read_tokens`` is the portion of ``input_tokens`` that was served
    from the provider's prompt cache; those tokens are billed at
    ``_CACHE_READ_DISCOUNT`` times the input rate rather than the full rate.
    Defaults to ``0`` for backward compatibility.

    Returns ``0.0`` if the model is not in the catalog.
    """
    info = get_model_info(model_id)
    if info is None:
        return 0.0
    # The harness reaches every provider (including Claude, via the yunwu /
    # OpenRouter gateways) over the OpenAI-compatible Chat Completions API, so
    # ``input_tokens`` (prompt_tokens) is INCLUSIVE of cache reads and
    # ``cache_read_tokens`` is a subset of it -- verified against the live
    # upstream (cached 64,551 < prompt 79,694).  Cap the cached bucket at input
    # so a stray count never makes uncached negative, bill it at the discounted
    # rate, and bill the remainder at the full input rate.
    cached = max(0, min(cache_read_tokens, input_tokens))
    uncached = input_tokens - cached
    input_cost = (uncached / 1000.0) * info.input_cost_per_1k
    cache_cost = (cached / 1000.0) * info.input_cost_per_1k * _CACHE_READ_DISCOUNT
    output_cost = (output_tokens / 1000.0) * info.output_cost_per_1k
    return input_cost + cache_cost + output_cost


def _has_rate(model_id: str) -> bool:
    info = get_model_info(model_id)
    return info is not None and (
        info.input_cost_per_1k > 0 or info.output_cost_per_1k > 0
    )


def estimate_call_cost(
    model_id: str,
    usage_model: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
) -> tuple[float, str | None]:
    """Price a call against the model that actually served it.

    Sessions run under a tier sentinel (``surogate`` / ``surogate-pro``),
    but the request is served by a concrete upstream model, reported back
    in the usage payload. The sentinels are in the catalog deliberately --
    the compressor needs their context window -- yet carry ``0.0`` prices,
    so pricing the sentinel yields no cost while token counters keep
    climbing. That is how a 1.48M-token session recorded
    ``estimated_cost_usd = 0``.

    Returns ``(cost, priced_model)``. ``priced_model`` is ``None`` when no
    source had a rate, which callers should surface: silently returning
    ``0.0`` is what hid the gap. Deliberately does NOT invent a rate -- a
    confidently wrong cost is worse than an obviously missing one.
    """
    for candidate in (usage_model, model_id):
        if candidate and _has_rate(candidate):
            return (
                estimate_cost(
                    candidate, input_tokens, output_tokens,
                    cache_read_tokens=cache_read_tokens,
                ),
                candidate,
            )

    # A call that consumed nothing is not a pricing gap.
    if not input_tokens and not output_tokens:
        return 0.0, (usage_model or model_id or None)
    return 0.0, None


# ---------------------------------------------------------------------------
# Context probe tiers
# ---------------------------------------------------------------------------

CONTEXT_PROBE_TIERS: list[int] = [
    128_000,
    64_000,
    32_000,
    16_000,
    8_000,
]

DEFAULT_FALLBACK_CONTEXT: int = CONTEXT_PROBE_TIERS[0]


def get_next_probe_tier(current_length: int) -> int | None:
    """Return the next lower probe tier, or ``None`` if already at minimum."""
    for tier in CONTEXT_PROBE_TIERS:
        if tier < current_length:
            return tier
    return None


def parse_context_limit_from_error(error_msg: str) -> int | None:
    """Try to extract the actual context limit from an API error message.

    Many providers include the limit in their error text, e.g.:

    - ``"maximum context length is 32768 tokens"``
    - ``"context_length_exceeded: 131072"``
    - ``"Maximum context size 32768 exceeded"``
    - ``"model's max context length is 65536"``
    """
    error_lower = error_msg.lower()
    patterns = [
        r'(?:max(?:imum)?|limit)\s*(?:context\s*)?(?:length|size|window)?\s*(?:is|of|:)?\s*(\d{4,})',
        r'context\s*(?:length|size|window)\s*(?:is|of|:)?\s*(\d{4,})',
        r'(\d{4,})\s*(?:token)?\s*(?:context|limit)',
        r'>\s*(\d{4,})\s*(?:max|limit|token)',
        r'(\d{4,})\s*(?:max(?:imum)?)\b',
    ]
    for pattern in patterns:
        match = re.search(pattern, error_lower)
        if match:
            limit = int(match.group(1))
            if 1024 <= limit <= 10_000_000:
                return limit
    return None
