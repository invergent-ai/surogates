"""Integration tests for the auto-think gate and SELF-DISCOVER preamble.

These exercise the two new ``AgentHarness`` methods at the harness
level (not just the underlying helpers), confirming that:

- ``_maybe_apply_thinking_gate`` mutates ``create_kwargs["extra_body"]``
  on easy turns only when the model supports the toggle.
- ``_maybe_apply_self_discover`` appends an ephemeral synthetic
  message on hard categories that are in ``SCAFFOLD_CATEGORIES`` and
  leaves the messages list untouched otherwise.
- Both methods degrade gracefully when classifier or scaffold builder
  raise.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from surogates.harness.budget import IterationBudget
from surogates.harness.expert_routing import HardTaskClassification
from surogates.harness.loop import AgentHarness
from surogates.harness.self_discover import ReasoningScaffold
from surogates.tools.registry import ToolRegistry


def _harness() -> AgentHarness:
    tenant = SimpleNamespace(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={},
        user_preferences={},
        asset_root="/tmp/test",
    )
    return AgentHarness(
        session_store=AsyncMock(),
        tool_registry=ToolRegistry(),
        llm_client=AsyncMock(),
        tenant=tenant,
        worker_id="worker",
        budget=IterationBudget(max_total=10),
        context_compressor=MagicMock(),
        prompt_builder=MagicMock(),
    )


def _easy() -> HardTaskClassification:
    return HardTaskClassification(required=False, category=None, reason="llm")


def _hard(category: str) -> HardTaskClassification:
    return HardTaskClassification(
        required=True, category=category, reason="llm", needs_scaffold=True,
    )


class TestThinkingGate:
    @pytest.mark.asyncio
    async def test_easy_turn_on_supported_model_disables_thinking(self):
        harness = _harness()
        create_kwargs = {"model": "surogate", "messages": []}
        messages = [{"role": "user", "content": "thanks!"}]

        with patch(
            "surogates.harness.loop.classify_hard_task_async",
            AsyncMock(return_value=_easy()),
        ):
            await harness._maybe_apply_thinking_gate(create_kwargs, messages)

        assert create_kwargs["extra_body"] == {
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "thinking_budget": 4096,
            "preserve_thinking": True,
        }

    @pytest.mark.asyncio
    async def test_hard_turn_leaves_thinking_default(self):
        harness = _harness()
        create_kwargs = {"model": "surogate", "messages": []}
        messages = [{"role": "user", "content": "Solve 3x + 7 = 22"}]

        with patch(
            "surogates.harness.loop.classify_hard_task_async",
            AsyncMock(return_value=_hard("math")),
        ):
            await harness._maybe_apply_thinking_gate(create_kwargs, messages)

        # enable_thinking is left at the provider default on hard turns,
        # but the harness defaults for thinking_budget / preserve_thinking
        # still land.
        assert create_kwargs["extra_body"] == {
            "thinking_budget": 4096,
            "preserve_thinking": True,
        }

    @pytest.mark.asyncio
    async def test_unsupported_model_skips_gate(self):
        harness = _harness()
        create_kwargs = {"model": "gpt-4o-mini", "messages": []}
        messages = [{"role": "user", "content": "hi"}]

        classifier = AsyncMock(return_value=_easy())
        with patch("surogates.harness.loop.classify_hard_task_async", classifier):
            await harness._maybe_apply_thinking_gate(create_kwargs, messages)

        assert "extra_body" not in create_kwargs
        # Classifier should be skipped entirely on unsupported models -- no
        # point spending an LLM call when the gate can't fire anyway.
        classifier.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_easy_turn_deep_merges_existing_extra_body(self):
        harness = _harness()
        create_kwargs = {
            "model": "zai-org/GLM-5.1",
            "messages": [],
            "extra_body": {
                "extra_headers": {"X-Trace": "abc"},
                "chat_template_kwargs": {"other_flag": True},
            },
        }
        messages = [{"role": "user", "content": "thanks!"}]

        with patch(
            "surogates.harness.loop.classify_hard_task_async",
            AsyncMock(return_value=_easy()),
        ):
            await harness._maybe_apply_thinking_gate(create_kwargs, messages)

        assert create_kwargs["extra_body"] == {
            "extra_headers": {"X-Trace": "abc"},
            "enable_thinking": False,
            "chat_template_kwargs": {
                "other_flag": True,
                "enable_thinking": False,
            },
            "thinking_budget": 4096,
            "preserve_thinking": True,
        }

    @pytest.mark.asyncio
    async def test_classifier_failure_leaves_default_thinking(self):
        harness = _harness()
        create_kwargs = {"model": "surogate", "messages": []}
        messages = [{"role": "user", "content": "anything"}]

        with patch(
            "surogates.harness.loop.classify_hard_task_async",
            AsyncMock(side_effect=RuntimeError("aux down")),
        ):
            await harness._maybe_apply_thinking_gate(create_kwargs, messages)

        # Classifier failure leaves enable_thinking at the provider default,
        # but harness budget / preserve defaults still apply.
        assert create_kwargs["extra_body"] == {
            "thinking_budget": 4096,
            "preserve_thinking": True,
        }

    @pytest.mark.asyncio
    async def test_empty_messages_skips_gate(self):
        harness = _harness()
        create_kwargs = {"model": "surogate", "messages": []}
        classifier = AsyncMock()

        with patch("surogates.harness.loop.classify_hard_task_async", classifier):
            await harness._maybe_apply_thinking_gate(create_kwargs, [])

        assert "extra_body" not in create_kwargs
        classifier.assert_not_awaited()


class TestSelfDiscover:
    @staticmethod
    def _scaffold() -> ReasoningScaffold:
        return ReasoningScaffold(
            relevant_modules=["problem_decomposition", "step_by_step"],
            structure={
                "understand_inputs": "Identify what the function receives.",
                "draft_implementation": "Write the recursion.",
                "verify_edge_cases": "Empty list, single element, deep nesting.",
            },
            pitfalls=[
                "Forgetting to handle non-list iterables.",
                "Accidentally flattening strings into characters.",
            ],
        )

    @pytest.mark.asyncio
    async def test_eligible_category_injects_scaffold(self):
        harness = _harness()
        api_messages = [
            {"role": "user", "content": "Write a flatten function."},
        ]
        create_kwargs = {"messages": list(api_messages)}

        with (
            patch(
                "surogates.harness.loop.classify_hard_task_async",
                AsyncMock(return_value=_hard("coding")),
            ),
            patch(
                "surogates.harness.loop.build_scaffold",
                AsyncMock(return_value=self._scaffold()),
            ),
        ):
            await harness._maybe_apply_self_discover(create_kwargs, api_messages)

        # Original api_messages is untouched -- mutation is on the copy.
        assert len(api_messages) == 1
        assert "<reasoning_scaffold>" not in api_messages[0]["content"]

        # The scaffold is merged into the existing user message rather
        # than appended as a separate synthetic message.
        assert len(create_kwargs["messages"]) == 1
        merged = create_kwargs["messages"][0]
        assert merged["role"] == "user"
        assert merged["_surogate_scaffold_merged"] is True
        assert merged["content"].startswith("Write a flatten function.")
        assert "<reasoning_scaffold>" in merged["content"]
        assert "draft_implementation" in merged["content"]
        assert "Accidentally flattening strings" in merged["content"]
        # Trailing imperative is gone — that was the source of the
        # per-iteration "The user is asking me to continue" loop.
        assert "Now produce the answer" not in merged["content"]

    @pytest.mark.asyncio
    async def test_excluded_category_terminal_skips_scaffold(self):
        harness = _harness()
        api_messages = [{"role": "user", "content": "git status"}]
        create_kwargs = {"messages": list(api_messages)}

        build = AsyncMock()
        with (
            patch(
                "surogates.harness.loop.classify_hard_task_async",
                AsyncMock(return_value=_hard("terminal")),
            ),
            patch("surogates.harness.loop.build_scaffold", build),
        ):
            await harness._maybe_apply_self_discover(create_kwargs, api_messages)

        assert create_kwargs["messages"] == api_messages
        build.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_chitchat_category_skips_scaffold(self):
        harness = _harness()
        api_messages = [{"role": "user", "content": "thanks!"}]
        create_kwargs = {"messages": list(api_messages)}

        build = AsyncMock()
        with (
            patch(
                "surogates.harness.loop.classify_hard_task_async",
                AsyncMock(return_value=_easy()),
            ),
            patch("surogates.harness.loop.build_scaffold", build),
        ):
            await harness._maybe_apply_self_discover(create_kwargs, api_messages)

        assert create_kwargs["messages"] == api_messages
        build.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_scaffold_builder_returns_none_skips_injection(self):
        harness = _harness()
        api_messages = [{"role": "user", "content": "Write a flatten function."}]
        create_kwargs = {"messages": list(api_messages)}

        with (
            patch(
                "surogates.harness.loop.classify_hard_task_async",
                AsyncMock(return_value=_hard("coding")),
            ),
            patch(
                "surogates.harness.loop.build_scaffold",
                AsyncMock(return_value=None),
            ),
        ):
            await harness._maybe_apply_self_discover(create_kwargs, api_messages)

        assert create_kwargs["messages"] == api_messages

    @pytest.mark.asyncio
    async def test_scaffold_builder_failure_skips_injection(self):
        harness = _harness()
        api_messages = [{"role": "user", "content": "Write a flatten function."}]
        create_kwargs = {"messages": list(api_messages)}

        with (
            patch(
                "surogates.harness.loop.classify_hard_task_async",
                AsyncMock(return_value=_hard("coding")),
            ),
            patch(
                "surogates.harness.loop.build_scaffold",
                AsyncMock(side_effect=RuntimeError("aux down")),
            ),
        ):
            await harness._maybe_apply_self_discover(create_kwargs, api_messages)

        assert create_kwargs["messages"] == api_messages

    @pytest.mark.asyncio
    async def test_classifier_failure_skips_scaffold(self):
        harness = _harness()
        api_messages = [{"role": "user", "content": "Write a flatten function."}]
        create_kwargs = {"messages": list(api_messages)}

        build = AsyncMock()
        with (
            patch(
                "surogates.harness.loop.classify_hard_task_async",
                AsyncMock(side_effect=RuntimeError("aux down")),
            ),
            patch("surogates.harness.loop.build_scaffold", build),
        ):
            await harness._maybe_apply_self_discover(create_kwargs, api_messages)

        assert create_kwargs["messages"] == api_messages
        build.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tenant_propagates_to_classifier_and_builder(self):
        harness = _harness()
        api_messages = [{"role": "user", "content": "Write a flatten function."}]
        create_kwargs = {"messages": list(api_messages)}

        classifier = AsyncMock(return_value=_hard("coding"))
        builder = AsyncMock(return_value=self._scaffold())

        with (
            patch("surogates.harness.loop.classify_hard_task_async", classifier),
            patch("surogates.harness.loop.build_scaffold", builder),
        ):
            await harness._maybe_apply_self_discover(create_kwargs, api_messages)

        # Both calls must receive the harness tenant -- that's how the
        # aux client picks up tenant-scoped credentials when one tenant
        # is using a different summary upstream than another.
        classifier.assert_awaited_once()
        assert classifier.call_args.kwargs["tenant"] is harness._tenant
        builder.assert_awaited_once()
        assert builder.call_args.kwargs["tenant"] is harness._tenant
        assert builder.call_args.kwargs["category"] == "coding"
