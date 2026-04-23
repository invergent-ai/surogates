"""Runtime model discovery from OpenAI-compatible ``/models`` endpoints.

The static catalog in :mod:`surogates.harness.model_metadata` covers the
handful of first-party models we've hand-curated.  For OpenAI-compatible
providers that route a much larger catalog (OpenRouter, LiteLLM proxy,
LM Studio, vLLM, Ollama), the static catalog misses every model except
a tiny subset, and :class:`~surogates.harness.context.ContextCompressor`
silently fell back to a 128k default — a confusing bug because the
compressor under- or over-estimates when to compact for most tenants.

This module fixes that by lazily fetching ``GET {base_url}/models`` the
first time an unknown model is queried for a given ``(base_url,
api_key)`` pair, parsing the response, and caching the resulting
``ModelInfo`` dictionary for the lifetime of the process.  The static
catalog still wins when there's overlap (human-curated pricing is more
accurate), and explicit config overrides win over both.

The cache is thread-safe and resilient to endpoint failures: a network
error, 4xx/5xx response, or malformed JSON is logged once as a warning
and returns an empty dict, so callers fall through to whatever default
the caller chose.  Subsequent lookups on the same ``(base_url, api_key)``
reuse the empty dict — we don't keep retrying a broken endpoint.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import httpx

from surogates.harness.model_metadata import ModelInfo

logger = logging.getLogger(__name__)

__all__ = ["ModelDiscoveryCache", "discover_model", "reset_discovery_cache"]

# Timeout for the one-shot ``/models`` fetch.  Provider endpoints are
# usually fast but shouldn't block worker startup for more than a few
# seconds.  On timeout we log a warning and treat the endpoint as empty.
_FETCH_TIMEOUT_SECONDS: float = 5.0

# Conservative default output cap when the provider doesn't report one.
# 4k is safe for virtually every model and consumers can override
# explicitly if they need more.
_DEFAULT_MAX_OUTPUT_TOKENS: int = 4096


class ModelDiscoveryCache:
    """Per-process cache of provider-reported model metadata.

    Keyed by ``(base_url, api_key)``: different credential pools may see
    different model availability, and we don't want one broken endpoint
    to contaminate another.  The cache is populated lazily on the first
    lookup for a given key; subsequent lookups for the same key are
    O(1) dict accesses.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], dict[str, ModelInfo]] = {}
        # Guards :attr:`_cache` and the "fetch in progress" flag.
        self._lock = threading.Lock()

    def lookup(
        self, model_id: str, *, base_url: str, api_key: str,
    ) -> ModelInfo | None:
        """Return the provider-reported :class:`ModelInfo` or ``None``."""
        if not base_url:
            return None
        key = (base_url, api_key)
        models = self._get_or_fetch(key)
        return models.get(model_id)

    def reset(self) -> None:
        """Drop all cached entries.  Primarily useful for tests."""
        with self._lock:
            self._cache.clear()

    # ------------------------------------------------------------------

    def _get_or_fetch(self, key: tuple[str, str]) -> dict[str, ModelInfo]:
        """Return the cached dict, fetching from the provider if needed.

        The lock is released during the HTTP call so concurrent lookups
        for *different* base URLs don't serialize.  A race where two
        threads fetch the same URL simultaneously is acceptable — both
        results are equivalent and the second one overwrites the first.
        """
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached

        base_url, api_key = key
        try:
            fetched = _fetch_models(base_url, api_key)
        except Exception as exc:
            logger.warning(
                "Model discovery failed for %s: %s — the compressor will "
                "fall back to its default context window for unknown "
                "models.  Set llm.models.<id>.context_window in config.yaml "
                "if you need an exact value.",
                base_url, exc,
            )
            fetched = {}

        with self._lock:
            # Still honor the first writer if another thread got there
            # first — the results are equivalent.
            self._cache.setdefault(key, fetched)
            return self._cache[key]


# Module-level singleton.  Tests can clear it via
# :func:`reset_discovery_cache` to isolate provider mocks.
_CACHE = ModelDiscoveryCache()


def discover_model(
    model_id: str, *, base_url: str, api_key: str,
) -> ModelInfo | None:
    """Convenience accessor for the module-level discovery cache."""
    return _CACHE.lookup(model_id, base_url=base_url, api_key=api_key)


def reset_discovery_cache() -> None:
    """Clear the module-level cache.  Exposed for tests."""
    _CACHE.reset()


# ---------------------------------------------------------------------------
# Provider fetch + parse
# ---------------------------------------------------------------------------


def _fetch_models(base_url: str, api_key: str) -> dict[str, ModelInfo]:
    """Fetch and parse ``{base_url}/models``.

    Accepts the OpenRouter/OpenAI-compatible shape::

        {"data": [{"id": "...", "context_length": N,
                   "pricing": {"prompt": "...", "completion": "..."},
                   "top_provider": {"max_completion_tokens": M}, ...}, ...]}

    Missing optional fields (pricing, max_completion_tokens) get safe
    defaults so the parse never fails on an otherwise-valid response.
    """
    url = base_url.rstrip("/") + "/models"
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    resp = httpx.get(url, headers=headers, timeout=_FETCH_TIMEOUT_SECONDS)
    resp.raise_for_status()
    payload = resp.json()

    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        logger.warning(
            "Model discovery response from %s missing 'data' list; got keys %s",
            url, list(payload.keys()) if isinstance(payload, dict) else type(payload),
        )
        return {}

    catalog: dict[str, ModelInfo] = {}
    for entry in entries:
        info = _parse_entry(entry)
        if info is not None:
            catalog[info.id] = info

    logger.info(
        "Discovered %d models from %s (examples: %s)",
        len(catalog), url,
        ", ".join(list(catalog.keys())[:3]) if catalog else "(none)",
    )
    return catalog


def _parse_entry(entry: Any) -> ModelInfo | None:
    """Parse a single ``/models`` entry into :class:`ModelInfo` or None."""
    if not isinstance(entry, dict):
        return None

    model_id = entry.get("id")
    if not isinstance(model_id, str) or not model_id:
        return None

    context_window = _coerce_int(entry.get("context_length"))
    if context_window is None:
        # Some LiteLLM / vLLM deployments nest the context under
        # top_provider.  Try that before giving up.
        top = entry.get("top_provider")
        if isinstance(top, dict):
            context_window = _coerce_int(top.get("context_length"))
    if context_window is None or context_window <= 0:
        # Nothing usable — skip rather than polluting the cache with a
        # bogus entry that would downgrade the compressor.
        return None

    max_output = _extract_max_output(entry)
    input_cost, output_cost = _extract_pricing(entry)

    return ModelInfo(
        id=model_id,
        context_window=context_window,
        max_output_tokens=max_output,
        input_cost_per_1k=input_cost,
        output_cost_per_1k=output_cost,
        # Provider /models endpoints don't advertise capability flags
        # reliably; assume "supports tools" (most do) and leave vision
        # to the caller.  The static catalog wins when it matters.
        supports_tools=True,
        supports_vision=False,
        supports_streaming=True,
    )


def _extract_max_output(entry: dict[str, Any]) -> int:
    """Pull ``max_completion_tokens`` / ``max_output_tokens`` from an entry."""
    top = entry.get("top_provider")
    if isinstance(top, dict):
        for key in ("max_completion_tokens", "max_output_tokens"):
            value = _coerce_int(top.get(key))
            if value:
                return value
    for key in ("max_completion_tokens", "max_output_tokens"):
        value = _coerce_int(entry.get(key))
        if value:
            return value
    return _DEFAULT_MAX_OUTPUT_TOKENS


def _extract_pricing(entry: dict[str, Any]) -> tuple[float, float]:
    """Pull per-1k-token input/output prices from an entry.

    Providers report prices per token (string-encoded floats).  Multiply
    by 1000 to match :class:`ModelInfo`'s ``*_per_1k`` convention.
    Returns ``(0.0, 0.0)`` when pricing is missing — downstream cost
    tracking degrades gracefully rather than erroring.
    """
    pricing = entry.get("pricing")
    if not isinstance(pricing, dict):
        return 0.0, 0.0
    return (
        _coerce_float(pricing.get("prompt")) * 1000.0,
        _coerce_float(pricing.get("completion")) * 1000.0,
    )


def _coerce_int(value: Any) -> int | None:
    """Best-effort conversion to a positive int."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    if isinstance(value, float):
        return int(value)
    return None


def _coerce_float(value: Any) -> float:
    """Best-effort conversion to a non-negative float.

    Returns ``0.0`` for missing / malformed values so pricing falls back
    cleanly without exceptions.
    """
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0
