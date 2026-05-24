"""AgentHarness accepts and stores an optional TurnSummarizer."""

from __future__ import annotations

import inspect

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

from surogates.harness.budget import IterationBudget
from surogates.harness.loop import AgentHarness


def _make_minimal_harness(**overrides):
    """Construct an AgentHarness with the minimum non-None scaffolding.

    Mirrors the pattern in tests/test_harness_resilience.py::_make_harness
    but stripped to the kwargs we need for the wiring contract test.
    """
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
        tenant=tenant,
        worker_id="test-worker",
        budget=IterationBudget(max_total=90),
        context_compressor=MagicMock(spec=ContextCompressor),
        prompt_builder=MagicMock(spec=PromptBuilder),
    )
    defaults.update(overrides)
    return AgentHarness(**defaults)


def test_agent_harness_accepts_turn_summarizer_kwarg() -> None:
    sig = inspect.signature(AgentHarness.__init__)
    assert "turn_summarizer" in sig.parameters
    param = sig.parameters["turn_summarizer"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is None


def test_agent_harness_defaults_turn_summarizer_to_none() -> None:
    harness = _make_minimal_harness()
    assert harness._turn_summarizer is None


def test_agent_harness_stores_turn_summarizer_when_provided() -> None:
    from surogates.harness.turn_summarizer import TurnSummarizer

    class _StubClient:
        chat = type(
            "X", (),
            {"completions": type(
                "Y", (),
                {"create": staticmethod(lambda **_: None)},
            )()},
        )

    summarizer = TurnSummarizer(
        summary_client=_StubClient(), summary_model="cheap",
    )
    harness = _make_minimal_harness(turn_summarizer=summarizer)
    assert harness._turn_summarizer is summarizer


def test_agent_harness_initializes_pending_summary_trackers() -> None:
    harness = _make_minimal_harness()
    assert harness._pending_iteration_summary_tasks == {}
    assert harness._completed_iteration_summaries == {}
