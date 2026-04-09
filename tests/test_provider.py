"""Tests for surogates.harness.provider -- LLM provider abstraction."""

from __future__ import annotations

import pytest

from surogates.harness.provider import (
    APIMode,
    anthropic_to_openai_response,
    call_anthropic_messages,
    detect_api_mode,
    openai_to_anthropic_messages,
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


# ---------------------------------------------------------------------------
# openai_to_anthropic_messages (Phase 2 stub)
# ---------------------------------------------------------------------------


class TestOpenAIToAnthropicMessages:
    def test_filters_system_messages(self) -> None:
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        result = openai_to_anthropic_messages(messages)
        roles = [m["role"] for m in result]
        assert "system" not in roles
        assert len(result) == 2

    def test_preserves_user_and_assistant(self) -> None:
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        result = openai_to_anthropic_messages(messages)
        assert result == messages

    def test_empty_messages(self) -> None:
        assert openai_to_anthropic_messages([]) == []


# ---------------------------------------------------------------------------
# anthropic_to_openai_response (Phase 2 stub)
# ---------------------------------------------------------------------------


class TestAnthropicToOpenAIResponse:
    def test_returns_dict(self) -> None:
        result = anthropic_to_openai_response("some response")
        assert isinstance(result, dict)
        assert result["role"] == "assistant"
        assert "content" in result


# ---------------------------------------------------------------------------
# call_anthropic_messages (Phase 2 stub)
# ---------------------------------------------------------------------------


class TestCallAnthropicMessages:
    @pytest.mark.asyncio()
    async def test_raises_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError, match="not yet implemented"):
            await call_anthropic_messages(
                client=None,
                model="claude-sonnet-4",
                messages=[],
                system="You are helpful.",
            )
