"""Memory tools add, replace, and remove entries through the configured memory provider."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from surogates.memory.store import (
    MemoryStore,
)
from surogates.memory.builtin import BuiltinMemoryProvider
from surogates.memory.manager import (
    MemoryManager,
)


class TestBuiltinMemoryProvider:
    """BuiltinMemoryProvider wraps MemoryStore."""


    def test_handle_tool_call_replace(self, tmp_path: Path):
        store = MemoryStore(memory_dir=tmp_path / "mem")
        provider = BuiltinMemoryProvider(store)
        provider.initialize()
        provider.handle_tool_call("memory", {
            "action": "add", "target": "memory", "content": "old note",
        })
        result_json = provider.handle_tool_call("memory", {
            "action": "replace",
            "target": "memory",
            "old_text": "old note",
            "content": "updated note",
        })
        result = json.loads(result_json)
        assert result["success"] is True

    def test_handle_tool_call_remove(self, tmp_path: Path):
        store = MemoryStore(memory_dir=tmp_path / "mem")
        provider = BuiltinMemoryProvider(store)
        provider.initialize()
        provider.handle_tool_call("memory", {
            "action": "add", "target": "memory", "content": "temp note",
        })
        result_json = provider.handle_tool_call("memory", {
            "action": "remove", "target": "memory", "old_text": "temp note",
        })
        result = json.loads(result_json)
        assert result["success"] is True


class TestMemoryToolHandlers:
    """Tool handlers in surogates.tools.builtin.memory."""

    @pytest.mark.asyncio
    async def test_memory_handler_with_manager(self, tmp_path: Path):
        from surogates.tools.builtin.memory import _memory_handler

        store = MemoryStore(memory_dir=tmp_path / "mem")
        manager = MemoryManager(store)
        manager.initialize_all()

        result_json = await _memory_handler(
            {"action": "add", "target": "memory", "content": "tool note"},
            memory_manager=manager,
        )
        result = json.loads(result_json)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_memory_handler_no_manager(self):
        from surogates.tools.builtin.memory import _memory_handler

        result_json = await _memory_handler(
            {"action": "add", "target": "memory", "content": "test"},
        )
        result = json.loads(result_json)
        assert result["success"] is False
