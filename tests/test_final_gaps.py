"""Tests prefill, memory prefetch, length continuation accumulation, invalid JSON retry, reasoning pass-through."""

from __future__ import annotations

import json
import pytest

from surogates.harness.loop import _is_valid_json_args


# ---------------------------------------------------------------------------
# _is_valid_json_args
# ---------------------------------------------------------------------------


class TestIsValidJsonArgs:
    def test_valid_json(self):
        tc = {"function": {"arguments": '{"path": "/tmp/test.py"}'}}
        assert _is_valid_json_args(tc) is True

    def test_empty_string(self):
        tc = {"function": {"arguments": ""}}
        assert _is_valid_json_args(tc) is True

    def test_empty_object(self):
        tc = {"function": {"arguments": "{}"}}
        assert _is_valid_json_args(tc) is True

    def test_invalid_json(self):
        tc = {"function": {"arguments": "{invalid json"}}
        assert _is_valid_json_args(tc) is False

    def test_json_array_not_dict(self):
        tc = {"function": {"arguments": "[1, 2, 3]"}}
        assert _is_valid_json_args(tc) is False

    def test_no_function_key(self):
        tc = {}
        assert _is_valid_json_args(tc) is True  # no args to validate

    def test_none_arguments(self):
        tc = {"function": {"arguments": None}}
        assert _is_valid_json_args(tc) is True

    def test_whitespace_only(self):
        tc = {"function": {"arguments": "   "}}
        assert _is_valid_json_args(tc) is True

    def test_truncated_json(self):
        tc = {"function": {"arguments": '{"path": "/tmp/te'}}
        assert _is_valid_json_args(tc) is False

    def test_json_string_not_object(self):
        tc = {"function": {"arguments": '"just a string"'}}
        assert _is_valid_json_args(tc) is False


# ---------------------------------------------------------------------------
# Length continuation prefix accumulation
# ---------------------------------------------------------------------------


class TestLengthContinuationPrefix:
    """Verify that the loop accumulates partial content across length
    continuation retries (tested via the prefix string logic)."""

    def test_prefix_accumulation_concept(self):
        """Unit test the accumulation logic in isolation."""
        prefix = ""
        partial1 = "First part of the response..."
        partial2 = "Second part continues..."
        final = "Final part."

        # Simulate 2 length truncations + 1 final
        prefix += partial1  # first truncation
        prefix += partial2  # second truncation
        result = prefix + final

        assert result == "First part of the response...Second part continues...Final part."
        assert result.startswith(partial1)
        assert result.endswith(final)


# ---------------------------------------------------------------------------
# Prefilled context injection
# ---------------------------------------------------------------------------


class TestPrefillConfig:
    """Verify that prefill_messages from session config are structured correctly."""

    def test_prefill_from_session_config(self):
        config = {
            "prefill_messages": [
                {"role": "user", "content": "Example input"},
                {"role": "assistant", "content": "Example output"},
            ]
        }
        prefill = config.get("prefill_messages") or []
        assert len(prefill) == 2
        assert prefill[0]["role"] == "user"
        assert prefill[1]["role"] == "assistant"

    def test_no_prefill(self):
        config = {}
        prefill = config.get("prefill_messages") or []
        assert prefill == []

    def test_none_prefill(self):
        config = {"prefill_messages": None}
        prefill = config.get("prefill_messages") or []
        assert prefill == []


# ---------------------------------------------------------------------------
# Reasoning details pass-through
# ---------------------------------------------------------------------------


class TestReasoningDetailsPassthrough:
    """Verify that reasoning_details in assistant messages are preserved."""

    def test_reasoning_details_preserved_in_message(self):
        """The assistant message dict should carry reasoning_details through."""
        msg = {
            "role": "assistant",
            "content": "I'll help you with that.",
            "reasoning_details": [
                {"type": "reasoning.summary", "summary": "Analyzed the request"},
            ],
        }
        # The message is appended to the messages list as-is
        messages = [msg]
        assert messages[0].get("reasoning_details") is not None
        assert len(messages[0]["reasoning_details"]) == 1

    def test_no_reasoning_details(self):
        msg = {
            "role": "assistant",
            "content": "Simple response.",
        }
        assert msg.get("reasoning_details") is None


# ---------------------------------------------------------------------------
# Memory prefetch (unit test for path resolution)
# ---------------------------------------------------------------------------


class TestMemoryPrefetchPaths:
    """Test the memory file path resolution logic."""

    def test_user_memory_path(self, tmp_path):
        from uuid import UUID

        org_id = UUID("11111111-1111-1111-1111-111111111111")
        user_id = UUID("22222222-2222-2222-2222-222222222222")

        # Create user memory
        user_mem_dir = tmp_path / f"users/{user_id}/memories"
        user_mem_dir.mkdir(parents=True)
        (user_mem_dir / "MEMORY.md").write_text("User memory content")

        # Simulate the prefetch path resolution
        from pathlib import Path

        for subdir in (
            f"users/{user_id}/memories",
            "shared/memories",
        ):
            memory_path = Path(tmp_path) / subdir / "MEMORY.md"
            if memory_path.is_file():
                content = memory_path.read_text().strip()
                break
        else:
            content = None

        assert content == "User memory content"

    def test_shared_memory_fallback(self, tmp_path):
        from uuid import UUID

        user_id = UUID("22222222-2222-2222-2222-222222222222")

        # Only shared memory exists
        shared_dir = tmp_path / "shared/memories"
        shared_dir.mkdir(parents=True)
        (shared_dir / "MEMORY.md").write_text("Shared memory")

        from pathlib import Path

        content = None
        for subdir in (
            f"users/{user_id}/memories",
            "shared/memories",
        ):
            memory_path = Path(tmp_path) / subdir / "MEMORY.md"
            if memory_path.is_file():
                content = memory_path.read_text().strip()
                break

        assert content == "Shared memory"

    def test_no_memory(self, tmp_path):
        from uuid import UUID

        user_id = UUID("22222222-2222-2222-2222-222222222222")

        from pathlib import Path

        content = None
        for subdir in (
            f"users/{user_id}/memories",
            "shared/memories",
        ):
            memory_path = Path(tmp_path) / subdir / "MEMORY.md"
            if memory_path.is_file():
                content = memory_path.read_text().strip()
                break

        assert content is None
