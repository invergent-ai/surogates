"""Tests for surogates.tools.registry.ToolRegistry and surogates.tools.router.ToolRouter."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from surogates.governance.policy import GovernanceGate
from surogates.sandbox.pool import SandboxPool
from surogates.tools.registry import ToolEntry, ToolRegistry, ToolSchema
from surogates.tools.router import ToolLocation, ToolRouter


# =========================================================================
# ToolRegistry
# =========================================================================


def _make_schema(name: str = "test_tool") -> ToolSchema:
    return ToolSchema(
        name=name,
        description=f"A test tool called {name}",
        parameters={
            "type": "object",
            "properties": {"input": {"type": "string"}},
        },
    )


async def _async_handler(arguments: dict, **kwargs) -> str:
    return f"result: {arguments.get('input', 'none')}"


def _sync_handler(arguments: dict, **kwargs) -> str:
    return f"sync_result: {arguments.get('input', 'none')}"


class TestToolRegistryBasics:
    """Registration, lookup, deregistration."""

    def test_register_and_get(self):
        reg = ToolRegistry()
        schema = _make_schema("my_tool")
        reg.register("my_tool", schema, _async_handler)
        entry = reg.get("my_tool")
        assert entry is not None
        assert entry.name == "my_tool"
        assert entry.schema.description == "A test tool called my_tool"

    def test_get_returns_none_for_unknown(self):
        reg = ToolRegistry()
        assert reg.get("nonexistent") is None

    def test_register_duplicate_raises(self):
        reg = ToolRegistry()
        schema = _make_schema("dup")
        reg.register("dup", schema, _async_handler)
        with pytest.raises(ValueError, match="already registered"):
            reg.register("dup", schema, _async_handler)

    def test_deregister(self):
        reg = ToolRegistry()
        schema = _make_schema("removable")
        reg.register("removable", schema, _async_handler)
        reg.deregister("removable")
        assert reg.get("removable") is None

    def test_has(self):
        reg = ToolRegistry()
        schema = _make_schema("x")
        reg.register("x", schema, _async_handler)
        assert reg.has("x") is True
        assert reg.has("y") is False

    def test_tool_names(self):
        reg = ToolRegistry()
        reg.register("a", _make_schema("a"), _async_handler)
        reg.register("b", _make_schema("b"), _async_handler)
        assert reg.tool_names == {"a", "b"}


class TestToolRegistrySchemas:
    """OpenAI-format schema export."""

    def test_get_schemas_returns_openai_format(self):
        reg = ToolRegistry()
        reg.register("tool_1", _make_schema("tool_1"), _async_handler)
        reg.register("tool_2", _make_schema("tool_2"), _async_handler)

        schemas = reg.get_schemas()
        assert len(schemas) == 2
        for s in schemas:
            assert s["type"] == "function"
            assert "function" in s
            assert "name" in s["function"]
            assert "description" in s["function"]
            assert "parameters" in s["function"]

    def test_get_schemas_filtered(self):
        reg = ToolRegistry()
        reg.register("a", _make_schema("a"), _async_handler)
        reg.register("b", _make_schema("b"), _async_handler)

        schemas = reg.get_schemas(names={"a"})
        assert len(schemas) == 1
        assert schemas[0]["function"]["name"] == "a"


class TestToolRegistryDispatch:
    """Async dispatch with result truncation."""

    @pytest.mark.asyncio
    async def test_dispatch_calls_handler(self):
        reg = ToolRegistry()
        reg.register("echo", _make_schema("echo"), _async_handler)

        result = await reg.dispatch("echo", {"input": "hello"})
        assert result == "result: hello"

    @pytest.mark.asyncio
    async def test_dispatch_truncates_large_results(self):
        async def big_handler(arguments: dict, **kwargs) -> str:
            return "x" * 100_000

        reg = ToolRegistry()
        reg.register(
            "big", _make_schema("big"), big_handler, max_result_size=1000
        )

        result = await reg.dispatch("big", {})
        assert len(result) < 100_000
        assert "[truncated at 1000 chars]" in result

    @pytest.mark.asyncio
    async def test_dispatch_handles_json_string_arguments(self):
        reg = ToolRegistry()
        reg.register("echo", _make_schema("echo"), _async_handler)

        result = await reg.dispatch("echo", '{"input": "from_json"}')
        assert result == "result: from_json"

    @pytest.mark.asyncio
    async def test_dispatch_handles_empty_json_string(self):
        reg = ToolRegistry()
        reg.register("echo", _make_schema("echo"), _async_handler)

        result = await reg.dispatch("echo", "  ")
        assert result == "result: none"

    @pytest.mark.asyncio
    async def test_dispatch_handles_invalid_json(self):
        reg = ToolRegistry()
        reg.register("echo", _make_schema("echo"), _async_handler)

        result = await reg.dispatch("echo", "not valid json{{{")
        parsed = json.loads(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_dispatch_unknown_tool_returns_error(self):
        reg = ToolRegistry()
        result = await reg.dispatch("nonexistent", {})
        parsed = json.loads(result)
        assert "error" in parsed
        assert "Unknown tool" in parsed["error"]

    @pytest.mark.asyncio
    async def test_dispatch_sync_handler(self):
        reg = ToolRegistry()
        reg.register(
            "sync_tool", _make_schema("sync_tool"), _sync_handler, is_async=False
        )

        result = await reg.dispatch("sync_tool", {"input": "test"})
        assert result == "sync_result: test"

    @pytest.mark.asyncio
    async def test_dispatch_handler_exception(self):
        async def failing_handler(arguments: dict, **kwargs) -> str:
            raise RuntimeError("boom")

        reg = ToolRegistry()
        reg.register("fail", _make_schema("fail"), failing_handler)

        result = await reg.dispatch("fail", {})
        parsed = json.loads(result)
        assert "error" in parsed
        assert "boom" in parsed["error"]


# =========================================================================
# ToolRouter
# =========================================================================


class TestToolRouterGovernance:
    """Governance check blocks denied tools."""

    @pytest.mark.asyncio
    async def test_governance_blocks_denied_tool(self):
        reg = ToolRegistry()
        reg.register("blocked_tool", _make_schema("blocked_tool"), _async_handler)

        gate = GovernanceGate(denied_tools={"blocked_tool"})
        pool = MagicMock(spec=SandboxPool)
        router = ToolRouter(reg, pool, gate)

        result = await router.execute(
            name="blocked_tool",
            arguments={"input": "test"},
            tenant=None,
            session_id=uuid4(),
        )
        parsed = json.loads(result)
        assert parsed["error"] == "policy_denied"

    @pytest.mark.asyncio
    async def test_governance_allows_permitted_tool(self):
        reg = ToolRegistry()
        # Register as a harness-local tool so it dispatches via registry.
        reg.register("memory", _make_schema("memory"), _async_handler)

        gate = GovernanceGate()  # open policy
        pool = MagicMock(spec=SandboxPool)
        router = ToolRouter(reg, pool, gate)

        result = await router.execute(
            name="memory",
            arguments={"input": "hi"},
            tenant=None,
            session_id=uuid4(),
        )
        assert result == "result: hi"


class TestToolRouterLocationResolution:
    """Tests for ToolRouter.resolve_location."""

    def test_harness_tools_resolve_to_harness(self):
        reg = ToolRegistry()
        pool = MagicMock(spec=SandboxPool)
        gate = GovernanceGate()
        router = ToolRouter(reg, pool, gate)

        for tool_name in ("memory", "skills_list", "skill_view", "skill_manage",
                          "session_search", "web_search", "web_extract",
                          "web_crawl", "clarify", "delegate_task", "todo", "process"):
            assert router.resolve_location(tool_name) == ToolLocation.HARNESS, (
                f"{tool_name} should resolve to HARNESS"
            )

    def test_sandbox_tools_resolve_to_sandbox(self):
        reg = ToolRegistry()
        pool = MagicMock(spec=SandboxPool)
        gate = GovernanceGate()
        router = ToolRouter(reg, pool, gate)

        for tool_name in ("terminal", "read_file", "write_file",
                          "patch", "search_files", "list_files"):
            assert router.resolve_location(tool_name) == ToolLocation.SANDBOX, (
                f"{tool_name} should resolve to SANDBOX"
            )

    def test_unknown_tool_defaults_to_sandbox(self):
        reg = ToolRegistry()
        pool = MagicMock(spec=SandboxPool)
        gate = GovernanceGate()
        router = ToolRouter(reg, pool, gate)

        assert router.resolve_location("totally_unknown") == ToolLocation.SANDBOX

    @pytest.mark.asyncio
    async def test_sandbox_tool_dispatches_to_pool(self):
        reg = ToolRegistry()
        pool = AsyncMock(spec=SandboxPool)
        pool.ensure = AsyncMock(return_value="sbx-123")
        pool.execute = AsyncMock(return_value='{"exit_code": 0, "stdout": "ok"}')
        gate = GovernanceGate()
        router = ToolRouter(reg, pool, gate)

        session_id = uuid4()
        result = await router.execute(
            name="terminal",
            arguments={"command": "echo hi"},
            tenant=None,
            session_id=session_id,
        )

        pool.ensure.assert_called_once()
        pool.execute.assert_called_once()
        assert "ok" in result
