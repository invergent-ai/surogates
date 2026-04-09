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
        info = get_model_info("gpt-4o")
        assert info is not None
        assert info.id == "gpt-4o"
        assert info.context_window == 128_000

    def test_returns_known_model_by_alias(self):
        info = get_model_info("sonnet")
        assert info is not None
        assert info.id == "claude-sonnet-4-20250514"

    def test_returns_none_for_unknown_model(self):
        assert get_model_info("nonexistent-model-xyz") is None

    def test_returns_claude_opus(self):
        info = get_model_info("claude-opus")
        assert info is not None
        assert info.id == "claude-opus-4-20250514"
        assert info.supports_vision is True

    def test_returns_deepseek_alias(self):
        info = get_model_info("deepseek")
        assert info is not None
        assert info.id == "deepseek-chat"


class TestModelCatalog:
    """Structural checks on the MODEL_CATALOG."""

    def test_catalog_has_expected_models(self):
        expected = {
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4.1",
            "claude-sonnet-4-20250514",
            "claude-opus-4-20250514",
            "deepseek-chat",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
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
        # gpt-4o: input=0.0025/1k, output=0.01/1k
        cost = estimate_cost("gpt-4o", input_tokens=1000, output_tokens=1000)
        expected = 0.0025 + 0.01
        assert abs(cost - expected) < 1e-9

    def test_zero_tokens(self):
        cost = estimate_cost("gpt-4o", input_tokens=0, output_tokens=0)
        assert cost == 0.0

    def test_unknown_model_returns_zero(self):
        cost = estimate_cost("nonexistent-model", input_tokens=1000, output_tokens=1000)
        assert cost == 0.0

    def test_large_token_counts(self):
        cost = estimate_cost("gpt-4o", input_tokens=100_000, output_tokens=50_000)
        # 100k * 0.0025/1k + 50k * 0.01/1k = 0.25 + 0.5 = 0.75
        assert abs(cost - 0.75) < 1e-9
