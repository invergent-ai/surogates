"""Tests for surogates.harness.provider -- LLM provider abstraction."""

from __future__ import annotations

from surogates.harness.provider import (
    APIMode,
    detect_api_mode,
)


# ---------------------------------------------------------------------------
# APIMode enum
# ---------------------------------------------------------------------------


class TestAPIMode:
    def test_values(self) -> None:
        assert APIMode.CHAT_COMPLETIONS == "chat_completions"
        assert APIMode.ANTHROPIC_MESSAGES == "anthropic_messages"

    def test_string_comparison(self) -> None:
        assert APIMode.CHAT_COMPLETIONS == "chat_completions"
        assert APIMode("chat_completions") == APIMode.CHAT_COMPLETIONS


# ---------------------------------------------------------------------------
# detect_api_mode
# ---------------------------------------------------------------------------


class TestDetectAPIMode:
    def test_default_returns_chat_completions(self) -> None:
        assert detect_api_mode("gpt-4o") == APIMode.CHAT_COMPLETIONS

    def test_anthropic_provider_returns_chat_completions_phase1(self) -> None:
        # Phase 1: all models go through chat_completions.
        result = detect_api_mode("claude-sonnet-4", provider="anthropic")
        assert result == APIMode.CHAT_COMPLETIONS

    def test_claude_with_anthropic_url_returns_chat_completions_phase1(self) -> None:
        result = detect_api_mode(
            "claude-opus-4-20250514",
            base_url="https://api.anthropic.com/v1",
        )
        assert result == APIMode.CHAT_COMPLETIONS

    def test_non_anthropic_provider(self) -> None:
        result = detect_api_mode("gpt-4o", provider="openai")
        assert result == APIMode.CHAT_COMPLETIONS

    def test_claude_without_anthropic_url(self) -> None:
        # Claude via OpenRouter or similar -- still chat_completions in Phase 1.
        result = detect_api_mode(
            "claude-sonnet-4",
            base_url="https://openrouter.ai/api/v1",
        )
        assert result == APIMode.CHAT_COMPLETIONS

    def test_none_provider_and_base_url(self) -> None:
        result = detect_api_mode("gpt-4o", base_url=None, provider=None)
        assert result == APIMode.CHAT_COMPLETIONS

    def test_deepseek_model(self) -> None:
        assert detect_api_mode("deepseek-chat") == APIMode.CHAT_COMPLETIONS

    def test_gemini_model(self) -> None:
        assert detect_api_mode("gemini-2.5-pro") == APIMode.CHAT_COMPLETIONS
