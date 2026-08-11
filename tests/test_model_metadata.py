"""Tests for surogates.harness.model_metadata."""

from __future__ import annotations

import pytest

from surogates.harness.model_metadata import (
    MODEL_CATALOG,
    ModelInfo,
    estimate_cost,
    estimate_tokens,
    get_model_info,
)


class TestGetModelInfo:
    """Lookup by exact ID and alias."""

    def test_returns_known_model_by_exact_id(self):
        info = get_model_info("openai/gpt-5.6-sol")
        assert info is not None
        assert info.id == "openai/gpt-5.6-sol"
        assert info.context_window == 1_050_000

    def test_returns_known_model_by_alias(self):
        info = get_model_info("sonnet")
        assert info is not None
        assert info.id == "anthropic/claude-sonnet-5"
        assert info.context_window == 1_000_000
        assert info.max_output_tokens == 128_000
        assert info.supports_vision is True

    def test_returns_none_for_unknown_model(self):
        assert get_model_info("nonexistent-model-xyz") is None

    def test_returns_claude_opus(self):
        info = get_model_info("claude-opus")
        assert info is not None
        assert info.id == "anthropic/claude-opus-5"
        assert info.context_window == 1_000_000
        assert info.max_output_tokens == 128_000
        assert info.supports_vision is True

    def test_retired_generations_resolve_to_nothing(self):
        """Retiring a model must retire its aliases with it -- a leftover
        alias is worse than a miss, because it silently prices the wrong
        model."""
        for model_id in (
            "claude-sonnet-4-6",
            "claude-opus-4-7",
            "claude-haiku-4-5",
            "haiku",
            "glm-5.1",
            "z-ai/glm-5.1",
            "kimi-k2.6",
            "qwen3.7-max",
            "gpt-5.4-nano",
            "gpt-5.5",
            "gpt-5.4-mini",
            "minimax-m3",
            "gemini-3-pro",
        ):
            assert get_model_info(model_id) is None, model_id

    def test_returns_deepseek_alias(self):
        info = get_model_info("deepseek")
        assert info is not None
        assert info.id == "deepseek/deepseek-v4-pro"
        assert info.context_window == 1_048_576
        assert info.max_output_tokens == 393_216
        assert info.supports_vision is False

        reasoner = get_model_info("deepseek-reasoner")
        assert reasoner is not None
        assert reasoner.id == "deepseek/deepseek-v4-flash-0731"
        assert reasoner.context_window == 1_048_576
        assert reasoner.max_output_tokens == 384_000
        assert reasoner.supports_vision is False

    def test_returns_qwen_max_by_alias(self):
        for model_id in ("qwen3.8-max", "qwen/qwen3.8-max", "@preset/qwen-3-8-max"):
            info = get_model_info(model_id)
            assert info is not None, model_id
            assert info.id == "qwen/qwen3.8-max"
            assert info.context_window == 1_000_000


class TestModelCatalog:
    """Structural checks on the MODEL_CATALOG."""

    def test_catalog_has_expected_models(self):
        expected = {
            "openai/gpt-5.6-luna",
            "openai/gpt-5.6-terra",
            "openai/gpt-5.6-sol",
            "anthropic/claude-sonnet-5",
            "anthropic/claude-opus-5",
            "deepseek/deepseek-v4-pro",
            "deepseek/deepseek-v4-flash-0731",
            "google/gemini-3-flash-preview",
            "google/gemini-3.5-flash",
            "z-ai/glm-5.2",
            "moonshotai/kimi-k3",
            "qwen/qwen3.8-max",
            "surogate",
            "surogate-pro",
        }
        assert expected.issubset(set(MODEL_CATALOG.keys()))

    def test_catalog_entries_are_model_info(self):
        for key, info in MODEL_CATALOG.items():
            assert isinstance(info, ModelInfo)
            assert info.id == key
            assert info.context_window > 0
            assert info.max_output_tokens > 0

    def test_all_entries_have_valid_costs(self):
        for info in MODEL_CATALOG.values():
            assert info.input_cost_per_1k >= 0
            assert info.output_cost_per_1k >= 0


class TestEstimateTokens:
    """Token estimation heuristic (~4 chars per token)."""

    def test_empty_string_returns_zero(self):
        assert estimate_tokens("") == 0

    def test_short_text(self):
        # "hello" = 5 chars -> ~1.25 tokens -> rounds to 1
        tokens = estimate_tokens("hello")
        assert tokens >= 1

    def test_rough_accuracy(self):
        # 400 chars of English prose -> ~100 tokens
        text = "word " * 80  # 400 chars
        tokens = estimate_tokens(text)
        assert 80 <= tokens <= 120  # Reasonable range

    def test_single_character(self):
        assert estimate_tokens("a") == 1

    def test_long_text(self):
        text = "a" * 4000  # ~1000 tokens
        tokens = estimate_tokens(text)
        assert 900 <= tokens <= 1100


class TestEstimateCost:
    """USD cost estimation."""

    def test_known_model_cost(self):
        # gpt-5.6-terra: input=0.001/1k, output=0.006/1k
        cost = estimate_cost("gpt-5.6-terra", input_tokens=1000, output_tokens=1000)
        expected = 0.001 + 0.006
        assert abs(cost - expected) < 1e-9

    def test_zero_tokens(self):
        cost = estimate_cost("gpt-5.6-terra", input_tokens=0, output_tokens=0)
        assert cost == 0.0

    def test_unknown_model_returns_zero(self):
        cost = estimate_cost("nonexistent-model", input_tokens=1000, output_tokens=1000)
        assert cost == 0.0

    def test_large_token_counts(self):
        cost = estimate_cost("gpt-5.6-terra", input_tokens=100_000, output_tokens=50_000)
        # 100k * 0.001/1k + 50k * 0.006/1k = 0.1 + 0.3 = 0.40
        assert abs(cost - 0.40) < 1e-9


class TestOpusCost:
    """claude-opus-5 pricing + cache-read discount (the prod Pro tier)."""

    def test_opus_in_catalog(self):
        info = get_model_info("claude-opus-5")
        assert info is not None
        assert info.id == "anthropic/claude-opus-5"
        # Opus 5 lists at $5 / $25 per 1M tokens.
        assert info.input_cost_per_1k == 0.005
        assert info.output_cost_per_1k == 0.025

    def test_opus_cost_is_nonzero(self):
        # Regression: the model was missing from the catalog, so estimate_cost
        # returned 0.0 and sessions.estimated_cost_usd never accrued.
        cost = estimate_cost("claude-opus-5", input_tokens=1000, output_tokens=1000)
        assert cost == pytest.approx(0.005 + 0.025)

    def test_cache_read_defaults_to_no_discount(self):
        # Backward compatible: omitting cache_read_tokens matches the old 3-arg
        # behaviour (whole prompt billed at the input rate).
        cost = estimate_cost("claude-opus-5", input_tokens=1000, output_tokens=0)
        assert cost == pytest.approx(0.005)

    def test_fully_cached_input_billed_at_cache_rate(self):
        # All input served from cache -> 10% of the input rate.
        cost = estimate_cost(
            "claude-opus-5", input_tokens=1000, output_tokens=0,
            cache_read_tokens=1000,
        )
        assert cost == pytest.approx(0.005 * 0.1)

    def test_partial_cache_splits_rates(self):
        # 400 of 1000 input tokens cached: 600 @ full + 400 @ 10%.
        cost = estimate_cost(
            "claude-opus-5", input_tokens=1000, output_tokens=0,
            cache_read_tokens=400,
        )
        expected = (600 / 1000) * 0.005 + (400 / 1000) * (0.005 * 0.1)
        assert cost == pytest.approx(expected)

    def test_cached_capped_at_input_tokens(self):
        # The harness uses OpenAI-compatible transport, so input_tokens is
        # INCLUSIVE of cache reads and cache_read is a subset of it.  A
        # cache_read larger than input_tokens is a data anomaly; cap it so the
        # cached bucket never exceeds the prompt and uncached never goes
        # negative.
        cost = estimate_cost(
            "claude-opus-5", input_tokens=1000, output_tokens=0,
            cache_read_tokens=5000,
        )
        assert cost == pytest.approx(0.005 * 0.1)

    def test_cache_discount_ignored_for_unknown_model(self):
        cost = estimate_cost(
            "nonexistent-model", input_tokens=1000, output_tokens=1000,
            cache_read_tokens=1000,
        )
        assert cost == 0.0


# ---------------------------------------------------------------------------
# Pricing the model that actually served the request
# ---------------------------------------------------------------------------


class TestEstimateCallCost:
    """Sessions run under a tier sentinel ("surogate"), but the request is
    served by a concrete upstream model reported back in usage.

    The sentinel is in the catalog on purpose -- the compressor needs its
    context window -- but carries 0.0 prices, so pricing the sentinel
    silently yields no cost while token counters keep climbing. Observed in
    PROD: 1.48M input tokens recorded against estimated_cost_usd = 0.
    """

    def test_prices_the_resolved_model_over_the_sentinel(self):
        from surogates.harness.model_metadata import estimate_call_cost

        cost, priced = estimate_call_cost(
            model_id="surogate", usage_model="anthropic/claude-sonnet-5",
            input_tokens=1_000_000, output_tokens=10_000,
        )
        assert priced == "anthropic/claude-sonnet-5"
        assert cost > 0

    def test_falls_back_to_the_sentinel_when_usage_model_is_absent(self):
        from surogates.harness.model_metadata import estimate_call_cost

        cost, priced = estimate_call_cost(
            model_id="openai/gpt-5.6-sol", usage_model=None,
            input_tokens=1_000, output_tokens=100,
        )
        assert priced == "openai/gpt-5.6-sol"
        assert cost > 0

    def test_reports_unpriced_when_no_source_has_a_rate(self):
        # The gap must be visible. Returning 0.0 with no signal is why a
        # 1.48M-token session showed zero cost and nobody noticed.
        from surogates.harness.model_metadata import estimate_call_cost

        cost, priced = estimate_call_cost(
            model_id="surogate", usage_model="some-unlisted-model-v9",
            input_tokens=1_000_000, output_tokens=10_000,
        )
        assert cost == 0.0
        assert priced is None

    def test_zero_token_call_is_not_reported_as_unpriced(self):
        from surogates.harness.model_metadata import estimate_call_cost

        cost, priced = estimate_call_cost(
            model_id="gpt-5.5", usage_model="gpt-5.5",
            input_tokens=0, output_tokens=0,
        )
        assert cost == 0.0
        assert priced == "gpt-5.5"


class TestClaudeCatalogRates:
    """Rates derived from yunwu's public /api/pricing for the default group.

    price per 1M = model_ratio * group_ratio * $2 (one-api convention);
    output = input * completion_ratio. Cross-checked against the recorded
    rate card: opus at ratio 2.5 gives $5/$25/$0.5/$6.25 per M for
    in/out/cache-read/cache-write, matching on all four.
    """

    def test_sonnet_5_rates(self):
        from surogates.harness.model_metadata import get_model_info

        i = get_model_info("claude-sonnet-5")
        assert i is not None
        assert i.input_cost_per_1k == 0.002    # ratio 1 -> $2/M (introductory)
        assert i.output_cost_per_1k == 0.010   # x5      -> $10/M

    def test_opus_5_rates(self):
        from surogates.harness.model_metadata import get_model_info

        i = get_model_info("claude-opus-5")
        assert i is not None
        assert i.input_cost_per_1k == 0.005    # ratio 2.5 -> $5/M
        assert i.output_cost_per_1k == 0.025   # x5        -> $25/M

    def test_context_windows_are_1m_not_the_sentinel_default(self):
        """Both ship a 1M window with 128k output.

        Copying the sentinel's 262k would make the compressor compress at
        a quarter of the real capacity.
        """
        from surogates.harness.model_metadata import get_model_info

        for name in ("claude-sonnet-5", "claude-opus-5"):
            i = get_model_info(name)
            assert i.context_window == 1_000_000, name
            assert i.max_output_tokens == 128_000, name

    def test_sentinel_session_now_prices_via_the_served_model(self):
        # The whole point: a session running as "surogate" served by
        # claude-sonnet-5 must accrue cost instead of silently zero.
        from surogates.harness.model_metadata import estimate_call_cost

        cost, priced = estimate_call_cost(
            "surogate", "claude-sonnet-5", 1_000_000, 10_000,
        )
        assert priced == "claude-sonnet-5"
        assert cost == pytest.approx(2.10)  # 1M*0.002 + 10k*0.010

    def test_cache_reads_are_discounted(self):
        from surogates.harness.model_metadata import estimate_call_cost

        cost, _ = estimate_call_cost(
            "surogate", "claude-sonnet-5", 1_000_000, 0,
            cache_read_tokens=1_000_000,
        )
        assert cost == pytest.approx(0.20)  # all cached: $2/M * 0.1


class TestCatalogIntegrity:
    """Structural invariants that silently degrade lookups when broken."""

    def test_every_alias_resolves_to_a_catalog_entry(self) -> None:
        """A dangling alias resolves to None, and the caller then falls back
        to a default context window and zero pricing -- no error, just a
        mis-sized context and an unbilled session."""
        from surogates.harness.model_metadata import MODEL_CATALOG, _ALIASES

        dangling = {a: t for a, t in _ALIASES.items() if t not in MODEL_CATALOG}
        assert not dangling, f"aliases pointing at missing entries: {dangling}"

    def test_no_alias_shadows_a_catalog_key(self) -> None:
        """get_model_info checks the catalog first, so such an alias is dead."""
        from surogates.harness.model_metadata import MODEL_CATALOG, _ALIASES

        shadowed = sorted(set(_ALIASES) & set(MODEL_CATALOG))
        assert not shadowed, f"aliases shadowed by catalog keys: {shadowed}"

    def test_entry_ids_match_their_catalog_key(self) -> None:
        from surogates.harness.model_metadata import MODEL_CATALOG

        mismatched = {k: v.id for k, v in MODEL_CATALOG.items() if v.id != k}
        assert not mismatched, f"ModelInfo.id != catalog key: {mismatched}"

    def test_configured_vision_model_is_priced(self) -> None:
        """vision_llm_model is gemini-3.5-flash; without an entry every
        vision call prices at zero."""
        from surogates.harness.model_metadata import get_model_info

        info = get_model_info("gemini-3.5-flash")
        assert info is not None
        assert info.input_cost_per_1k > 0
        assert info.supports_vision is True

    def test_tier_sentinels_carry_no_rate(self) -> None:
        """Sentinels must stay unpriced so estimate_call_cost falls through
        to the model that actually served the call."""
        from surogates.harness.model_metadata import MODEL_CATALOG

        for sentinel in ("surogate", "surogate-pro"):
            info = MODEL_CATALOG[sentinel]
            assert info.input_cost_per_1k == 0.0
            assert info.output_cost_per_1k == 0.0
