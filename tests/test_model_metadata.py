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
        info = get_model_info("gpt-5.5")
        assert info is not None
        assert info.id == "gpt-5.5"
        assert info.context_window == 1_050_000

    def test_returns_known_model_by_alias(self):
        info = get_model_info("sonnet")
        assert info is not None
        assert info.id == "claude-sonnet-4-6"
        assert info.context_window == 1_000_000
        assert info.max_output_tokens == 64_000
        assert info.supports_vision is True

    def test_returns_none_for_unknown_model(self):
        assert get_model_info("nonexistent-model-xyz") is None

    def test_returns_claude_opus(self):
        info = get_model_info("claude-opus")
        assert info is not None
        assert info.id == "claude-opus-4-7"
        assert info.context_window == 1_000_000
        assert info.max_output_tokens == 128_000
        assert info.supports_vision is True

    def test_returns_claude_haiku(self):
        info = get_model_info("haiku")
        assert info is not None
        assert info.id == "claude-haiku-4-5"
        assert info.context_window == 200_000
        assert info.max_output_tokens == 64_000
        assert info.supports_vision is True

    def test_returns_deepseek_alias(self):
        info = get_model_info("deepseek")
        assert info is not None
        assert info.id == "deepseek/deepseek-v4-pro"
        assert info.context_window == 1_000_000
        assert info.max_output_tokens == 384_000
        assert info.supports_vision is False

        reasoner = get_model_info("deepseek-reasoner")
        assert reasoner is not None
        assert reasoner.id == "deepseek/deepseek-v4-flash"
        assert reasoner.context_window == 1_000_000
        assert reasoner.max_output_tokens == 384_000
        assert reasoner.supports_vision is False

    def test_returns_glm_5_1_aliases_as_text_only(self):
        for model_id in (
            "glm-5.1",
            "glm5.1",
            "zai/glm-5.1",
            "z-ai/glm-5.1",
            "zai-org/GLM-5.1",
        ):
            info = get_model_info(model_id)
            assert info is not None
            assert info.id == "glm-5.1"
            assert info.context_window == 202_752
            assert info.max_output_tokens == 131_072
            assert info.supports_vision is False


class TestModelCatalog:
    """Structural checks on the MODEL_CATALOG."""

    def test_catalog_has_expected_models(self):
        expected = {
            "gpt-5.5",
            "gpt-5.4-mini",
            "gpt-5.4-nano",
            "claude-sonnet-4-6",
            "claude-opus-4-7",
            "claude-haiku-4-5",
            "deepseek/deepseek-v4-pro",
            "deepseek/deepseek-v4-flash",
            "gemini-3-pro",
            "gemini-3-flash",
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
        # gpt-5.4-nano: input=0.0002/1k, output=0.00125/1k
        cost = estimate_cost("gpt-5.4-nano", input_tokens=1000, output_tokens=1000)
        expected = 0.0002 + 0.00125
        assert abs(cost - expected) < 1e-9

    def test_zero_tokens(self):
        cost = estimate_cost("gpt-5.4-nano", input_tokens=0, output_tokens=0)
        assert cost == 0.0

    def test_unknown_model_returns_zero(self):
        cost = estimate_cost("nonexistent-model", input_tokens=1000, output_tokens=1000)
        assert cost == 0.0

    def test_large_token_counts(self):
        cost = estimate_cost("gpt-5.4-nano", input_tokens=100_000, output_tokens=50_000)
        # 100k * 0.0002/1k + 50k * 0.00125/1k = 0.02 + 0.0625 = 0.0825
        assert abs(cost - 0.0825) < 1e-9
