"""Tests for the 5 nice-to-have gaps: thinking prefill, content-with-tools
fallback, incomplete scratchpad, content coercion, error diagnostics."""

from __future__ import annotations

import pytest

from surogates.harness.message_utils import coerce_message_content
from surogates.harness.reasoning import (
    ContentWithToolsCache,
    has_incomplete_scratchpad,
    is_thinking_only_response,
)
from surogates.harness.resilience import get_error_diagnostic


# ---------------------------------------------------------------------------
# is_thinking_only_response
# ---------------------------------------------------------------------------


class TestIsThinkingOnlyResponse:
    def test_reasoning_field_no_content(self):
        msg = {"role": "assistant", "content": "", "reasoning": "I thought about it"}
        assert is_thinking_only_response(msg) is True

    def test_reasoning_field_with_content(self):
        msg = {"role": "assistant", "content": "Here's my answer", "reasoning": "Thought"}
        assert is_thinking_only_response(msg) is False

    def test_inline_think_blocks_only(self):
        msg = {"role": "assistant", "content": "<think>reasoning here</think>"}
        assert is_thinking_only_response(msg) is True

    def test_inline_think_with_visible_content(self):
        msg = {"role": "assistant", "content": "<think>reasoning</think> Actual answer"}
        assert is_thinking_only_response(msg) is False

    def test_no_reasoning_no_content(self):
        msg = {"role": "assistant", "content": ""}
        assert is_thinking_only_response(msg) is False

    def test_with_tool_calls(self):
        msg = {"role": "assistant", "content": "", "reasoning": "Thought", "tool_calls": [{"id": "1"}]}
        assert is_thinking_only_response(msg) is False

    def test_reasoning_content_field(self):
        msg = {"role": "assistant", "content": None, "reasoning_content": "Deep thought"}
        assert is_thinking_only_response(msg) is True

    def test_reasoning_details(self):
        msg = {"role": "assistant", "content": "", "reasoning_details": [{"content": "Analyzed"}]}
        assert is_thinking_only_response(msg) is True

    def test_normal_response(self):
        msg = {"role": "assistant", "content": "Normal answer"}
        assert is_thinking_only_response(msg) is False


# ---------------------------------------------------------------------------
# ContentWithToolsCache
# ---------------------------------------------------------------------------


class TestContentWithToolsCache:
    def test_cache_content_with_tools(self):
        cache = ContentWithToolsCache()
        msg = {"content": "Here's what I found", "tool_calls": [{"id": "1"}]}
        cache.maybe_cache(msg)
        assert cache.get_fallback() == "Here's what I found"

    def test_no_cache_without_tools(self):
        cache = ContentWithToolsCache()
        msg = {"content": "Just text"}
        cache.maybe_cache(msg)
        assert cache.get_fallback() is None

    def test_no_cache_without_content(self):
        cache = ContentWithToolsCache()
        msg = {"content": "", "tool_calls": [{"id": "1"}]}
        cache.maybe_cache(msg)
        assert cache.get_fallback() is None

    def test_clear(self):
        cache = ContentWithToolsCache()
        msg = {"content": "Text", "tool_calls": [{"id": "1"}]}
        cache.maybe_cache(msg)
        cache.clear()
        assert cache.get_fallback() is None

    def test_latest_cache_wins(self):
        cache = ContentWithToolsCache()
        cache.maybe_cache({"content": "First", "tool_calls": [{"id": "1"}]})
        cache.maybe_cache({"content": "Second", "tool_calls": [{"id": "2"}]})
        assert cache.get_fallback() == "Second"


# ---------------------------------------------------------------------------
# has_incomplete_scratchpad
# ---------------------------------------------------------------------------


class TestIncompleteScatchpad:
    def test_unclosed_scratchpad(self):
        msg = {"content": "<REASONING_SCRATCHPAD>I was thinking about..."}
        assert has_incomplete_scratchpad(msg) is True

    def test_closed_scratchpad(self):
        msg = {"content": "<REASONING_SCRATCHPAD>Thought</REASONING_SCRATCHPAD> Answer"}
        assert has_incomplete_scratchpad(msg) is False

    def test_no_scratchpad(self):
        msg = {"content": "Normal response"}
        assert has_incomplete_scratchpad(msg) is False

    def test_empty_content(self):
        msg = {"content": ""}
        assert has_incomplete_scratchpad(msg) is False

    def test_none_content(self):
        msg = {"content": None}
        assert has_incomplete_scratchpad(msg) is False

    def test_multiple_scratchpads_one_unclosed(self):
        msg = {"content": "<REASONING_SCRATCHPAD>A</REASONING_SCRATCHPAD><REASONING_SCRATCHPAD>B"}
        assert has_incomplete_scratchpad(msg) is True

    def test_case_insensitive(self):
        msg = {"content": "<reasoning_scratchpad>lowercase thinking"}
        assert has_incomplete_scratchpad(msg) is True


# ---------------------------------------------------------------------------
# coerce_message_content
# ---------------------------------------------------------------------------


class TestCoerceMessageContent:
    def test_string_unchanged(self):
        msg = {"content": "Hello"}
        result = coerce_message_content(msg)
        assert result["content"] == "Hello"

    def test_none_unchanged(self):
        msg = {"content": None}
        result = coerce_message_content(msg)
        assert result["content"] is None

    def test_list_of_text_dicts(self):
        msg = {"content": [{"type": "text", "text": "Part 1"}, {"type": "text", "text": "Part 2"}]}
        result = coerce_message_content(msg)
        assert result["content"] == "Part 1\nPart 2"

    def test_list_with_strings(self):
        msg = {"content": ["Hello", "World"]}
        result = coerce_message_content(msg)
        assert result["content"] == "Hello\nWorld"

    def test_dict_with_text(self):
        msg = {"content": {"text": "Extracted text"}}
        result = coerce_message_content(msg)
        assert result["content"] == "Extracted text"

    def test_dict_without_text(self):
        msg = {"content": {"key": "value"}}
        result = coerce_message_content(msg)
        assert '"key"' in result["content"]
        assert '"value"' in result["content"]

    def test_integer_content(self):
        msg = {"content": 42}
        result = coerce_message_content(msg)
        assert result["content"] == "42"

    def test_empty_list(self):
        msg = {"content": []}
        result = coerce_message_content(msg)
        assert result["content"] == ""

    def test_list_with_content_key(self):
        msg = {"content": [{"content": "From content key"}]}
        result = coerce_message_content(msg)
        assert result["content"] == "From content key"


# ---------------------------------------------------------------------------
# get_error_diagnostic
# ---------------------------------------------------------------------------


class TestGetErrorDiagnostic:
    def test_401_openai(self):
        result = get_error_diagnostic(401, Exception("Unauthorized"), provider="openai")
        assert "OPENAI_API_KEY" in result
        assert "401" in result

    def test_401_anthropic(self):
        result = get_error_diagnostic(401, Exception("Unauthorized"), provider="anthropic")
        assert "ANTHROPIC_API_KEY" in result

    def test_401_openrouter(self):
        result = get_error_diagnostic(401, Exception("Unauthorized"), provider="openrouter")
        assert "OPENROUTER_API_KEY" in result

    def test_401_unknown_provider(self):
        result = get_error_diagnostic(401, Exception("Unauthorized"), provider="unknown")
        assert "Authentication failed" in result

    def test_402_billing(self):
        result = get_error_diagnostic(402, Exception("Payment Required"), provider="openai")
        assert "billing" in result.lower() or "credits" in result.lower()

    def test_429_rate_limit(self):
        result = get_error_diagnostic(429, Exception("Rate limited"))
        assert "Rate limited" in result

    def test_500_server_error(self):
        result = get_error_diagnostic(500, Exception("Internal Server Error"))
        assert "internal server error" in result.lower()

    def test_none_status_code(self):
        result = get_error_diagnostic(None, ConnectionError("Connection refused"))
        assert "Network error" in result
        assert "ConnectionError" in result

    def test_unknown_status_code(self):
        result = get_error_diagnostic(418, Exception("I'm a teapot"))
        assert "418" in result
        assert "teapot" in result

    def test_no_provider(self):
        result = get_error_diagnostic(401, Exception("Unauthorized"))
        assert "Authentication failed" in result
