"""Tests for surogates.harness.sanitize module.

Covers: surrogate sanitization, tool pair fixup, budget warning stripping,
tool call deduplication, and delegate task capping.
"""

from __future__ import annotations

import json

import pytest

from surogates.harness.sanitize import (
    cap_delegate_calls,
    deduplicate_tool_calls,
    sanitize_messages,
    sanitize_surrogates,
    sanitize_tool_pairs,
    strip_budget_warnings,
)


# ---------------------------------------------------------------------------
# sanitize_surrogates
# ---------------------------------------------------------------------------


class TestSanitizeSurrogates:
    def test_no_surrogates_unchanged(self) -> None:
        text = "Hello, world!"
        assert sanitize_surrogates(text) == text

    def test_lone_surrogate_replaced(self) -> None:
        text = "Hello\ud800World"
        result = sanitize_surrogates(text)
        assert "\ud800" not in result
        assert "\ufffd" in result
        assert result == "Hello\ufffdWorld"

    def test_multiple_surrogates_replaced(self) -> None:
        text = "\ud800\udbff\udc00\udfff"
        result = sanitize_surrogates(text)
        assert result == "\ufffd\ufffd\ufffd\ufffd"

    def test_empty_string(self) -> None:
        assert sanitize_surrogates("") == ""

    def test_normal_unicode_preserved(self) -> None:
        text = "Caf\u00e9 \u2603 \U0001F600"
        assert sanitize_surrogates(text) == text


# ---------------------------------------------------------------------------
# sanitize_messages
# ---------------------------------------------------------------------------


class TestSanitizeMessages:
    def test_sanitizes_string_content(self) -> None:
        messages = [{"role": "user", "content": "Hello\ud800"}]
        result = sanitize_messages(messages)
        assert result[0]["content"] == "Hello\ufffd"

    def test_sanitizes_multimodal_content(self) -> None:
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Text\ud800here"},
                {"type": "image_url", "image_url": {"url": "http://example.com"}},
            ],
        }]
        result = sanitize_messages(messages)
        assert result[0]["content"][0]["text"] == "Text\ufffdhere"

    def test_sanitizes_tool_call_arguments(self) -> None:
        messages = [{
            "role": "assistant",
            "tool_calls": [{
                "id": "tc1",
                "function": {"name": "test", "arguments": '{"key": "val\ud800ue"}'},
            }],
        }]
        result = sanitize_messages(messages)
        args = result[0]["tool_calls"][0]["function"]["arguments"]
        assert "\ud800" not in args
        assert "\ufffd" in args

    def test_no_surrogates_passes_through(self) -> None:
        messages = [
            {"role": "user", "content": "clean text"},
            {"role": "assistant", "content": "clean reply"},
        ]
        result = sanitize_messages(messages)
        assert result[0]["content"] == "clean text"
        assert result[1]["content"] == "clean reply"

    def test_content_dict_key_sanitized(self) -> None:
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "content": "inner\ud800text"},
            ],
        }]
        result = sanitize_messages(messages)
        assert result[0]["content"][0]["content"] == "inner\ufffdtext"

    def test_empty_messages(self) -> None:
        assert sanitize_messages([]) == []

    def test_non_string_content_unchanged(self) -> None:
        messages = [{"role": "user", "content": None}]
        result = sanitize_messages(messages)
        assert result[0]["content"] is None


# ---------------------------------------------------------------------------
# sanitize_tool_pairs
# ---------------------------------------------------------------------------


class TestSanitizeToolPairs:
    def test_valid_pairs_unchanged(self) -> None:
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "tc1"}]},
            {"role": "tool", "tool_call_id": "tc1", "content": "ok"},
        ]
        result = sanitize_tool_pairs(messages)
        assert len(result) == 2

    def test_orphaned_tool_result_dropped(self) -> None:
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "tool", "tool_call_id": "orphan1", "content": "no match"},
        ]
        result = sanitize_tool_pairs(messages)
        assert len(result) == 1
        assert result[0]["role"] == "user"

    def test_stub_injected_for_missing_result(self) -> None:
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "tc1"}]},
            # No matching tool result
        ]
        result = sanitize_tool_pairs(messages)
        assert len(result) == 2
        assert result[1]["role"] == "tool"
        assert result[1]["tool_call_id"] == "tc1"
        assert "unavailable" in result[1]["content"]

    def test_invalid_role_dropped(self) -> None:
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "invalid_role", "content": "dropped"},
            {"role": "assistant", "content": "reply"},
        ]
        result = sanitize_tool_pairs(messages)
        assert len(result) == 2
        roles = [m["role"] for m in result]
        assert "invalid_role" not in roles

    def test_call_id_alias_supported(self) -> None:
        """Tool calls may use ``call_id`` instead of ``id``."""
        messages = [
            {"role": "assistant", "tool_calls": [{"call_id": "tc1"}]},
            {"role": "tool", "tool_call_id": "tc1", "content": "ok"},
        ]
        result = sanitize_tool_pairs(messages)
        assert len(result) == 2

    def test_mixed_valid_and_orphaned(self) -> None:
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "tc1"}, {"id": "tc2"}]},
            {"role": "tool", "tool_call_id": "tc1", "content": "ok"},
            {"role": "tool", "tool_call_id": "tc2", "content": "ok"},
            {"role": "tool", "tool_call_id": "orphan", "content": "stale"},
        ]
        result = sanitize_tool_pairs(messages)
        tool_ids = [m["tool_call_id"] for m in result if m.get("role") == "tool"]
        assert "orphan" not in tool_ids
        assert "tc1" in tool_ids
        assert "tc2" in tool_ids

    def test_empty_messages(self) -> None:
        assert sanitize_tool_pairs([]) == []

    def test_developer_role_preserved(self) -> None:
        messages = [{"role": "developer", "content": "system note"}]
        result = sanitize_tool_pairs(messages)
        assert len(result) == 1

    def test_stub_placed_after_assistant_message(self) -> None:
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "tool_calls": [{"id": "tc1"}]},
            {"role": "user", "content": "follow up"},
        ]
        result = sanitize_tool_pairs(messages)
        # Stub should be injected right after the assistant message
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert result[2]["role"] == "tool"
        assert result[2]["tool_call_id"] == "tc1"
        assert result[3]["role"] == "user"


# ---------------------------------------------------------------------------
# strip_budget_warnings
# ---------------------------------------------------------------------------


class TestStripBudgetWarnings:
    def test_strips_text_budget_warning(self) -> None:
        messages = [{
            "role": "tool",
            "tool_call_id": "tc1",
            "content": "result\n\n[BUDGET WARNING: 5 iterations remaining out of 90. Wrap up your work.]",
        }]
        result = strip_budget_warnings(messages)
        assert "BUDGET" not in result[0]["content"]
        assert result[0]["content"] == "result"

    def test_strips_json_budget_warning(self) -> None:
        content = json.dumps({"output": "ok", "_budget_warning": "5 remaining"})
        messages = [{"role": "tool", "tool_call_id": "tc1", "content": content}]
        result = strip_budget_warnings(messages)
        parsed = json.loads(result[0]["content"])
        assert "_budget_warning" not in parsed
        assert parsed["output"] == "ok"

    def test_non_tool_messages_untouched(self) -> None:
        messages = [
            {"role": "user", "content": "[BUDGET WARNING: fake]"},
            {"role": "assistant", "content": "[BUDGET WARNING: also fake]"},
        ]
        result = strip_budget_warnings(messages)
        assert "[BUDGET WARNING:" in result[0]["content"]
        assert "[BUDGET WARNING:" in result[1]["content"]

    def test_no_warning_passes_through(self) -> None:
        messages = [{"role": "tool", "tool_call_id": "tc1", "content": "clean"}]
        result = strip_budget_warnings(messages)
        assert result[0]["content"] == "clean"

    def test_empty_messages(self) -> None:
        assert strip_budget_warnings([]) == []

    def test_strips_budget_without_warning_word(self) -> None:
        messages = [{
            "role": "tool",
            "tool_call_id": "tc1",
            "content": "ok [BUDGET: some info]",
        }]
        result = strip_budget_warnings(messages)
        assert result[0]["content"] == "ok"


# ---------------------------------------------------------------------------
# deduplicate_tool_calls
# ---------------------------------------------------------------------------


class TestDeduplicateToolCalls:
    def test_no_duplicates_unchanged(self) -> None:
        tool_calls = [
            {"id": "1", "function": {"name": "a", "arguments": '{"x": 1}'}},
            {"id": "2", "function": {"name": "b", "arguments": '{"x": 2}'}},
        ]
        result = deduplicate_tool_calls(tool_calls)
        assert len(result) == 2

    def test_exact_duplicates_removed(self) -> None:
        tool_calls = [
            {"id": "1", "function": {"name": "a", "arguments": '{"x": 1}'}},
            {"id": "2", "function": {"name": "a", "arguments": '{"x": 1}'}},
            {"id": "3", "function": {"name": "a", "arguments": '{"x": 1}'}},
        ]
        result = deduplicate_tool_calls(tool_calls)
        assert len(result) == 1
        assert result[0]["id"] == "1"  # first one kept

    def test_same_name_different_args_kept(self) -> None:
        tool_calls = [
            {"id": "1", "function": {"name": "a", "arguments": '{"x": 1}'}},
            {"id": "2", "function": {"name": "a", "arguments": '{"x": 2}'}},
        ]
        result = deduplicate_tool_calls(tool_calls)
        assert len(result) == 2

    def test_empty_list(self) -> None:
        assert deduplicate_tool_calls([]) == []

    def test_single_call(self) -> None:
        tool_calls = [{"id": "1", "function": {"name": "a", "arguments": "{}"}}]
        result = deduplicate_tool_calls(tool_calls)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# cap_delegate_calls
# ---------------------------------------------------------------------------


class TestCapDelegateCalls:
    def test_under_cap_unchanged(self) -> None:
        tool_calls = [
            {"id": "1", "function": {"name": "delegate_task", "arguments": "{}"}},
            {"id": "2", "function": {"name": "delegate_task", "arguments": "{}"}},
        ]
        result = cap_delegate_calls(tool_calls)
        delegate_count = sum(
            1 for tc in result if tc["function"]["name"] == "delegate_task"
        )
        assert delegate_count == 2

    def test_over_cap_truncated(self) -> None:
        tool_calls = [
            {"id": str(i), "function": {"name": "delegate_task", "arguments": "{}"}}
            for i in range(10)
        ]
        result = cap_delegate_calls(tool_calls, max_delegates=3)
        delegate_count = sum(
            1 for tc in result if tc["function"]["name"] == "delegate_task"
        )
        assert delegate_count == 3

    def test_non_delegate_calls_preserved(self) -> None:
        tool_calls = [
            {"id": "1", "function": {"name": "file_read", "arguments": "{}"}},
            {"id": "2", "function": {"name": "delegate_task", "arguments": "{}"}},
            {"id": "3", "function": {"name": "delegate_task", "arguments": "{}"}},
            {"id": "4", "function": {"name": "delegate_task", "arguments": "{}"}},
            {"id": "5", "function": {"name": "memory_read", "arguments": "{}"}},
        ]
        result = cap_delegate_calls(tool_calls, max_delegates=1)
        names = [tc["function"]["name"] for tc in result]
        assert names.count("delegate_task") == 1
        assert "file_read" in names
        assert "memory_read" in names

    def test_at_cap_unchanged(self) -> None:
        tool_calls = [
            {"id": str(i), "function": {"name": "delegate_task", "arguments": "{}"}}
            for i in range(5)
        ]
        result = cap_delegate_calls(tool_calls, max_delegates=5)
        assert len(result) == 5

    def test_empty_list(self) -> None:
        assert cap_delegate_calls([]) == []

    def test_no_delegates(self) -> None:
        tool_calls = [
            {"id": "1", "function": {"name": "file_read", "arguments": "{}"}},
        ]
        result = cap_delegate_calls(tool_calls)
        assert len(result) == 1

    def test_default_cap_is_five(self) -> None:
        tool_calls = [
            {"id": str(i), "function": {"name": "delegate_task", "arguments": "{}"}}
            for i in range(8)
        ]
        result = cap_delegate_calls(tool_calls)
        delegate_count = sum(
            1 for tc in result if tc["function"]["name"] == "delegate_task"
        )
        assert delegate_count == 5
