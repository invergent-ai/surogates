"""Tests for the repair_tool_name function and its integration with find_invalid_tool_calls.

Covers: exact match after normalization, fuzzy matching, and integration
with the invalid tool call detection pipeline.
"""

from __future__ import annotations

import json

import pytest

from surogates.harness.resilience import find_invalid_tool_calls, repair_tool_name
from surogates.tools.registry import ToolRegistry, ToolSchema


def _make_registry(*names: str) -> ToolRegistry:
    """Create a registry with the given tool names."""
    reg = ToolRegistry()
    for name in names:
        reg.register(
            name,
            ToolSchema(name=name, description=f"Tool {name}", parameters={}),
            handler=lambda x: x,
        )
    return reg


# ---------------------------------------------------------------------------
# repair_tool_name
# ---------------------------------------------------------------------------


class TestRepairToolName:
    def test_exact_lowercase_match(self) -> None:
        reg = _make_registry("file_read", "file_write")
        assert repair_tool_name("File_Read", reg) == "file_read"

    def test_hyphen_to_underscore(self) -> None:
        reg = _make_registry("file_read", "file_write")
        assert repair_tool_name("file-read", reg) == "file_read"

    def test_space_to_underscore(self) -> None:
        reg = _make_registry("file_read", "file_write")
        assert repair_tool_name("file read", reg) == "file_read"

    def test_mixed_normalization(self) -> None:
        reg = _make_registry("web_search", "memory_read")
        assert repair_tool_name("Web-Search", reg) == "web_search"

    def test_fuzzy_match_typo(self) -> None:
        reg = _make_registry("file_read", "file_write", "memory_read")
        result = repair_tool_name("file_raed", reg)
        assert result == "file_read"

    def test_fuzzy_match_close(self) -> None:
        reg = _make_registry("delegate_task", "memory_read")
        result = repair_tool_name("delegat_task", reg)
        assert result == "delegate_task"

    def test_no_match_returns_none(self) -> None:
        reg = _make_registry("file_read", "file_write")
        assert repair_tool_name("completely_unknown", reg) is None

    def test_empty_registry(self) -> None:
        reg = ToolRegistry()
        assert repair_tool_name("anything", reg) is None

    def test_already_correct(self) -> None:
        reg = _make_registry("file_read")
        # Already correct after lowercase
        assert repair_tool_name("file_read", reg) == "file_read"


# ---------------------------------------------------------------------------
# find_invalid_tool_calls with repair integration
# ---------------------------------------------------------------------------


class TestFindInvalidToolCallsWithRepair:
    def test_repairs_tool_name_in_place(self) -> None:
        reg = _make_registry("file_read", "file_write")
        tool_calls = [
            {"id": "1", "function": {"name": "File-Read", "arguments": "{}"}},
        ]
        invalid = find_invalid_tool_calls(tool_calls, reg)
        assert invalid == []
        # Name should have been repaired in-place
        assert tool_calls[0]["function"]["name"] == "file_read"

    def test_fuzzy_repair_in_place(self) -> None:
        reg = _make_registry("file_read", "memory_read")
        tool_calls = [
            {"id": "1", "function": {"name": "file_raed", "arguments": "{}"}},
        ]
        invalid = find_invalid_tool_calls(tool_calls, reg)
        assert invalid == []
        assert tool_calls[0]["function"]["name"] == "file_read"

    def test_unrepairable_still_invalid(self) -> None:
        reg = _make_registry("file_read")
        tool_calls = [
            {"id": "1", "function": {"name": "zzz_unknown_zzz", "arguments": "{}"}},
        ]
        invalid = find_invalid_tool_calls(tool_calls, reg)
        assert len(invalid) == 1
        assert "Unknown tool" in invalid[0][1]

    def test_malformed_json_still_invalid_after_repair(self) -> None:
        reg = _make_registry("file_read")
        tool_calls = [
            {"id": "1", "function": {"name": "file_read", "arguments": "{bad}"}},
        ]
        invalid = find_invalid_tool_calls(tool_calls, reg)
        assert len(invalid) == 1
        assert "Malformed JSON" in invalid[0][1]

    def test_mixed_valid_repaired_and_invalid(self) -> None:
        reg = _make_registry("file_read", "file_write")
        tool_calls = [
            {"id": "1", "function": {"name": "file_read", "arguments": "{}"}},      # valid
            {"id": "2", "function": {"name": "File-Write", "arguments": "{}"}},     # repairable
            {"id": "3", "function": {"name": "total_junk_xyz", "arguments": "{}"}},  # invalid
        ]
        invalid = find_invalid_tool_calls(tool_calls, reg)
        assert len(invalid) == 1
        assert invalid[0][0]["id"] == "3"
        assert tool_calls[1]["function"]["name"] == "file_write"
