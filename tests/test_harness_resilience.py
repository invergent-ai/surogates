"""Tests for production-hardening features in surogates.harness.loop.

Covers: retry helpers, response validation, tool result truncation,
length continuation, budget pressure warnings, invalid tool call recovery,
and the retry/fallback integration.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from surogates.harness.budget import IterationBudget
from surogates.harness.credentials import CredentialPool, PooledCredential
from surogates.harness.llm_call import (
    extract_retry_after as _extract_retry_after,
    extract_status_code as _extract_status_code,
    interruptible_sleep as _interruptible_sleep,
    is_transient_error as _is_transient_error,
)
from surogates.harness.loop import AgentHarness


# ---------------------------------------------------------------------------
# _extract_status_code
# ---------------------------------------------------------------------------


class TestExtractStatusCode:
    def test_from_status_code_attr(self) -> None:
        exc = Exception("rate limited")
        exc.status_code = 429  # type: ignore[attr-defined]
        assert _extract_status_code(exc) == 429

    def test_from_response_attr(self) -> None:
        exc = Exception("server error")
        exc.response = SimpleNamespace(status_code=502)  # type: ignore[attr-defined]
        assert _extract_status_code(exc) == 502

    def test_no_status_returns_none(self) -> None:
        exc = Exception("generic error")
        assert _extract_status_code(exc) is None

    def test_string_status_code_converted(self) -> None:
        exc = Exception("err")
        exc.status_code = "503"  # type: ignore[attr-defined]
        assert _extract_status_code(exc) == 503


# ---------------------------------------------------------------------------
# _is_transient_error
# ---------------------------------------------------------------------------


class TestIsTransientError:
    def test_connection_error(self) -> None:
        assert _is_transient_error(ConnectionError("reset")) is True

    def test_timeout_error(self) -> None:
        assert _is_transient_error(TimeoutError("timed out")) is True

    def test_generic_exception_not_transient(self) -> None:
        assert _is_transient_error(Exception("unknown")) is False

    def test_value_error_not_transient(self) -> None:
        assert _is_transient_error(ValueError("bad value")) is False


# ---------------------------------------------------------------------------
# _extract_retry_after
# ---------------------------------------------------------------------------


class TestExtractRetryAfter:
    def test_from_headers(self) -> None:
        exc = Exception("rate limited")
        exc.response = SimpleNamespace(  # type: ignore[attr-defined]
            headers={"retry-after": "5.0"},
        )
        assert _extract_retry_after(exc) == 5.0

    def test_from_body(self) -> None:
        exc = Exception("rate limited")
        exc.response = SimpleNamespace(headers={})  # type: ignore[attr-defined]
        exc.body = {"error": {"retry_after": 10.0}}  # type: ignore[attr-defined]
        assert _extract_retry_after(exc) == 10.0

    def test_capped_at_120(self) -> None:
        exc = Exception("rate limited")
        exc.response = SimpleNamespace(  # type: ignore[attr-defined]
            headers={"retry-after": "300"},
        )
        assert _extract_retry_after(exc) == 120.0

    def test_no_retry_after(self) -> None:
        exc = Exception("rate limited")
        assert _extract_retry_after(exc) is None

    def test_invalid_header_returns_none(self) -> None:
        exc = Exception("rate limited")
        exc.response = SimpleNamespace(  # type: ignore[attr-defined]
            headers={"retry-after": "not-a-number"},
        )
        assert _extract_retry_after(exc) is None


# ---------------------------------------------------------------------------
# _interruptible_sleep
# ---------------------------------------------------------------------------


class TestInterruptibleSleep:
    async def test_sleeps_for_duration(self) -> None:
        import time
        start = time.monotonic()
        await _interruptible_sleep(0.3, lambda: False)
        elapsed = time.monotonic() - start
        assert elapsed >= 0.25

    async def test_interrupted_early(self) -> None:
        import time
        start = time.monotonic()
        await _interruptible_sleep(5.0, lambda: True)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0

    async def test_non_callable_flag_true(self) -> None:
        import time
        start = time.monotonic()
        await _interruptible_sleep(5.0, True)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0

    async def test_non_callable_flag_false(self) -> None:
        import time
        start = time.monotonic()
        await _interruptible_sleep(0.3, False)
        elapsed = time.monotonic() - start
        assert elapsed >= 0.25


# ---------------------------------------------------------------------------
# AgentHarness -- helpers that don't need a full loop
# ---------------------------------------------------------------------------


def _make_harness(**overrides: Any) -> AgentHarness:
    """Create a minimal AgentHarness with mocked dependencies."""
    from surogates.harness.context import ContextCompressor
    from surogates.harness.prompt import PromptBuilder
    from surogates.tenant.context import TenantContext
    from surogates.tools.registry import ToolRegistry

    tenant = TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root="/tmp/test",
    )

    defaults = dict(
        session_store=AsyncMock(),
        tool_registry=ToolRegistry(),
        llm_client=AsyncMock(),
        sandbox_pool=AsyncMock(),
        tenant=tenant,
        worker_id="test-worker",
        budget=IterationBudget(max_total=90),
        context_compressor=MagicMock(spec=ContextCompressor),
        prompt_builder=MagicMock(spec=PromptBuilder),
    )
    defaults.update(overrides)

    return AgentHarness(
        session_store=defaults["session_store"],
        tool_registry=defaults["tool_registry"],
        llm_client=defaults["llm_client"],
        sandbox_pool=defaults["sandbox_pool"],
        tenant=defaults["tenant"],
        worker_id=defaults["worker_id"],
        budget=defaults["budget"],
        context_compressor=defaults["context_compressor"],
        prompt_builder=defaults["prompt_builder"],
    )


class TestFindInvalidToolCalls:
    """Tests for _find_invalid_tool_calls."""

    def test_no_invalid_calls(self) -> None:
        from surogates.tools.registry import ToolRegistry, ToolSchema
        reg = ToolRegistry()
        reg.register("my_tool", ToolSchema(name="my_tool", description="test", parameters={}), lambda x: x)
        harness = _make_harness(tool_registry=reg)

        tool_calls = [
            {"id": "1", "function": {"name": "my_tool", "arguments": '{"x": 1}'}},
        ]
        invalid = harness._find_invalid_tool_calls(tool_calls)
        assert invalid == []

    def test_unknown_tool_name(self) -> None:
        harness = _make_harness()
        tool_calls = [
            {"id": "1", "function": {"name": "nonexistent_tool", "arguments": "{}"}},
        ]
        invalid = harness._find_invalid_tool_calls(tool_calls)
        assert len(invalid) == 1
        tc, error_msg = invalid[0]
        assert tc["id"] == "1"
        assert "Unknown tool" in error_msg

    def test_malformed_json_arguments(self) -> None:
        from surogates.tools.registry import ToolRegistry, ToolSchema
        reg = ToolRegistry()
        reg.register("my_tool", ToolSchema(name="my_tool", description="test", parameters={}), lambda x: x)
        harness = _make_harness(tool_registry=reg)

        tool_calls = [
            {"id": "1", "function": {"name": "my_tool", "arguments": "{bad json}"}},
        ]
        invalid = harness._find_invalid_tool_calls(tool_calls)
        assert len(invalid) == 1
        tc, error_msg = invalid[0]
        assert "Malformed JSON" in error_msg

    def test_empty_arguments_is_valid(self) -> None:
        from surogates.tools.registry import ToolRegistry, ToolSchema
        reg = ToolRegistry()
        reg.register("my_tool", ToolSchema(name="my_tool", description="test", parameters={}), lambda x: x)
        harness = _make_harness(tool_registry=reg)

        tool_calls = [
            {"id": "1", "function": {"name": "my_tool", "arguments": ""}},
        ]
        invalid = harness._find_invalid_tool_calls(tool_calls)
        assert invalid == []


class TestInjectBudgetWarning:
    """Tests for _inject_budget_warning (two-tier system)."""

    def test_no_warning_when_budget_healthy(self) -> None:
        harness = _make_harness(budget=IterationBudget(max_total=100))
        results = [{"role": "tool", "tool_call_id": "1", "content": "ok"}]
        out = harness._inject_budget_warning(results)
        assert "[BUDGET" not in out[0]["content"]

    def test_caution_injected_at_70_percent(self) -> None:
        budget = IterationBudget(max_total=100)
        # Consume 80 iterations -> 80% used (caution tier)
        for _ in range(80):
            budget.consume()
        harness = _make_harness(budget=budget)

        results = [{"role": "tool", "tool_call_id": "1", "content": "ok"}]
        out = harness._inject_budget_warning(results)
        assert "[BUDGET:" in out[0]["content"]
        assert "Start consolidating your work" in out[0]["content"]
        assert "20 iterations left" in out[0]["content"]

    def test_warning_injected_at_90_percent(self) -> None:
        budget = IterationBudget(max_total=100)
        # Consume 95 iterations -> 95% used (warning tier)
        for _ in range(95):
            budget.consume()
        harness = _make_harness(budget=budget)

        results = [{"role": "tool", "tool_call_id": "1", "content": "ok"}]
        out = harness._inject_budget_warning(results)
        assert "[BUDGET WARNING:" in out[0]["content"]
        assert "Provide your final response NOW" in out[0]["content"]

    def test_no_warning_on_empty_results(self) -> None:
        budget = IterationBudget(max_total=10)
        for _ in range(9):
            budget.consume()
        harness = _make_harness(budget=budget)
        out = harness._inject_budget_warning([])
        assert out == []

    def test_warning_appended_to_last_result(self) -> None:
        budget = IterationBudget(max_total=10)
        # Consume 9 iterations -> 90% used (warning tier)
        for _ in range(9):
            budget.consume()
        harness = _make_harness(budget=budget)

        results = [
            {"role": "tool", "tool_call_id": "1", "content": "first"},
            {"role": "tool", "tool_call_id": "2", "content": "second"},
        ]
        out = harness._inject_budget_warning(results)
        # Warning only on the last result
        assert "[BUDGET" not in out[0]["content"]
        assert "[BUDGET WARNING:" in out[1]["content"]


class TestTryActivateFallback:
    """Tests for _try_activate_fallback."""

    def test_no_fallbacks_returns_false(self) -> None:
        harness = _make_harness()
        assert harness._try_activate_fallback() is False

    def test_activates_first_fallback(self) -> None:
        harness = _make_harness()
        harness._fallback_chain = [
            {"provider": "anthropic", "model": "claude-sonnet-4-20250514", "api_key": "sk-fb"},
        ]
        result = harness._try_activate_fallback()
        assert result is True
        assert harness._current_model == "claude-sonnet-4-20250514"
        assert harness._fallback_activated is True

    def test_skips_invalid_fallback(self) -> None:
        harness = _make_harness()
        harness._fallback_chain = [
            {"provider": "", "model": ""},  # invalid
            {"provider": "openai", "model": "gpt-4o-mini"},
        ]
        result = harness._try_activate_fallback()
        assert result is True
        assert harness._current_model == "gpt-4o-mini"

    def test_exhausted_fallbacks_returns_false(self) -> None:
        harness = _make_harness()
        harness._fallback_chain = [
            {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
        ]
        harness._try_activate_fallback()  # consume the only fallback
        assert harness._try_activate_fallback() is False


class TestTryRotateCredential:
    """Tests for _try_rotate_credential."""

    def test_no_pool_returns_false(self) -> None:
        harness = _make_harness()
        assert harness._try_rotate_credential(429, Exception("rate limited")) is False

    def test_rotates_to_next_credential(self) -> None:
        harness = _make_harness()
        harness._credential_pool = CredentialPool([
            PooledCredential(id="a", api_key="sk-a", label="key-a"),
            PooledCredential(id="b", api_key="sk-b", label="key-b"),
        ])
        result = harness._try_rotate_credential(429, Exception("rate limited"))
        assert result is True

    def test_no_more_credentials_returns_false(self) -> None:
        harness = _make_harness()
        harness._credential_pool = CredentialPool([
            PooledCredential(id="a", api_key="sk-a"),
        ])
        result = harness._try_rotate_credential(429, Exception("rate limited"))
        assert result is False


class TestHarnessInitNewFields:
    """Verify that new __init__ fields are properly initialized."""

    def test_credential_pool_default_none(self) -> None:
        harness = _make_harness()
        assert harness._credential_pool is None

    def test_fallback_chain_default_empty(self) -> None:
        harness = _make_harness()
        assert harness._fallback_chain == []

    def test_fallback_index_default_zero(self) -> None:
        harness = _make_harness()
        assert harness._fallback_index == 0

    def test_fallback_activated_default_false(self) -> None:
        harness = _make_harness()
        assert harness._fallback_activated is False

    def test_primary_config_default_none(self) -> None:
        harness = _make_harness()
        assert harness._primary_config is None

    def test_current_model_default_none(self) -> None:
        harness = _make_harness()
        assert harness._current_model is None
