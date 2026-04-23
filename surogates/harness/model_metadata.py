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

MODEL_CATALOG: dict[str, ModelInfo] = {
    # --- OpenAI -----------------------------------------------------------
    "gpt-4o": ModelInfo(
        id="gpt-4o",
        context_window=128_000,
        max_output_tokens=16_384,
        input_cost_per_1k=0.0025,
        output_cost_per_1k=0.01,
        supports_vision=True,
    ),
    "gpt-4o-mini": ModelInfo(
        id="gpt-4o-mini",
        context_window=128_000,
        max_output_tokens=16_384,
        input_cost_per_1k=0.00015,
        output_cost_per_1k=0.0006,
        supports_vision=True,
    ),
    "gpt-4.1": ModelInfo(
        id="gpt-4.1",
        context_window=1_047_576,
        max_output_tokens=32_768,
        input_cost_per_1k=0.002,
        output_cost_per_1k=0.008,
        supports_vision=True,
    ),
    "gpt-4.1-mini": ModelInfo(
        id="gpt-4.1-mini",
        context_window=1_047_576,
        max_output_tokens=32_768,
        input_cost_per_1k=0.0004,
        output_cost_per_1k=0.0016,
        supports_vision=True,
    ),
    "gpt-4.1-nano": ModelInfo(
        id="gpt-4.1-nano",
        context_window=1_047_576,
        max_output_tokens=32_768,
        input_cost_per_1k=0.0001,
        output_cost_per_1k=0.0004,
        supports_vision=True,
    ),
    "o3": ModelInfo(
        id="o3",
        context_window=200_000,
        max_output_tokens=100_000,
        input_cost_per_1k=0.01,
        output_cost_per_1k=0.04,
        supports_vision=True,
    ),
    "o3-mini": ModelInfo(
        id="o3-mini",
        context_window=200_000,
        max_output_tokens=100_000,
        input_cost_per_1k=0.0011,
        output_cost_per_1k=0.0044,
        supports_vision=False,
    ),
    "o4-mini": ModelInfo(
        id="o4-mini",
        context_window=200_000,
        max_output_tokens=100_000,
        input_cost_per_1k=0.0011,
        output_cost_per_1k=0.0044,
        supports_vision=True,
    ),
    # --- Anthropic --------------------------------------------------------
    "claude-sonnet-4-20250514": ModelInfo(
        id="claude-sonnet-4-20250514",
        context_window=200_000,
        max_output_tokens=16_000,
        input_cost_per_1k=0.003,
        output_cost_per_1k=0.015,
        supports_vision=True,
    ),
    "claude-opus-4-20250514": ModelInfo(
        id="claude-opus-4-20250514",
        context_window=200_000,
        max_output_tokens=32_000,
        input_cost_per_1k=0.015,
        output_cost_per_1k=0.075,
        supports_vision=True,
    ),
    "claude-haiku-4-5-20251001": ModelInfo(
        id="claude-haiku-4-5-20251001",
        context_window=200_000,
        max_output_tokens=8_192,
        input_cost_per_1k=0.0008,
        output_cost_per_1k=0.004,
        supports_vision=True,
    ),
    # --- DeepSeek ---------------------------------------------------------
    "deepseek-chat": ModelInfo(
        id="deepseek-chat",
        context_window=64_000,
        max_output_tokens=8_192,
        input_cost_per_1k=0.00014,
        output_cost_per_1k=0.00028,
        supports_vision=False,
    ),
    "deepseek-reasoner": ModelInfo(
        id="deepseek-reasoner",
        context_window=64_000,
        max_output_tokens=8_192,
        input_cost_per_1k=0.00055,
        output_cost_per_1k=0.0022,
        supports_vision=False,
    ),
    # --- Google -----------------------------------------------------------
    "gemini-2.5-pro": ModelInfo(
        id="gemini-2.5-pro",
        context_window=1_048_576,
        max_output_tokens=65_536,
        input_cost_per_1k=0.00125,
        output_cost_per_1k=0.01,
        supports_vision=True,
    ),
    "gemini-2.5-flash": ModelInfo(
        id="gemini-2.5-flash",
        context_window=1_048_576,
        max_output_tokens=65_536,
        input_cost_per_1k=0.00015,
        output_cost_per_1k=0.0006,
        supports_vision=True,
    ),
}

# Build alias lookup: allow matching by short prefix or common alias.
_ALIASES: dict[str, str] = {
    "claude-sonnet": "claude-sonnet-4-20250514",
    "claude-opus": "claude-opus-4-20250514",
    "claude-haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-20250514",
    "opus": "claude-opus-4-20250514",
    "haiku": "claude-haiku-4-5-20251001",
    "deepseek": "deepseek-chat",
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


def estimate_cost(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Estimate the USD cost for a single LLM call.

    Returns ``0.0`` if the model is not in the catalog.
    """
    info = get_model_info(model_id)
    if info is None:
        return 0.0
    input_cost = (input_tokens / 1000.0) * info.input_cost_per_1k
    output_cost = (output_tokens / 1000.0) * info.output_cost_per_1k
    return input_cost + output_cost


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
