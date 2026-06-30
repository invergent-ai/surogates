"""Tests for the hard-task classifier and advisor preflight.

Auto-routing to experts was dropped (see
``docs/superpowers/specs/2026-05-23-expert-mechanism-resurrection-design.md``);
``TestDeadHelpersRemoved`` guards against the deleted helpers
creeping back.  The classifier now drives only the hidden advisor
preflight.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from surogates.harness.budget import IterationBudget
from surogates.harness.loop import AgentHarness
from surogates.session.events import EventType
from surogates.session.models import Event, Session
from surogates.tools.registry import ToolRegistry


def _session() -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        id=uuid4(),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        agent_id="default",
        channel="web",
        status="active",
        model="default-model",
        config={"temperature": 0.7},
        created_at=now,
        updated_at=now,
    )


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
        advisor_client=AsyncMock(),
        advisor_model="advisor-model",
    )


class TestHardTaskClassification:
    def test_routes_coding(self):
        from surogates.harness.expert_routing import classify_hard_task

        result = classify_hard_task("Fix this Python traceback in app.py")

        assert result.required is True
        assert result.category == "debugging"

    def test_routes_terminal(self):
        from surogates.harness.expert_routing import classify_hard_task

        result = classify_hard_task("Run pytest and then inspect the failing test")

        assert result.required is True
        assert result.category == "terminal"

    def test_routes_math(self):
        from surogates.harness.expert_routing import classify_hard_task

        result = classify_hard_task("Solve 3x + 7 = 22 and explain each step")

        assert result.required is True
        assert result.category == "math"

    def test_skips_generic_chat(self):
        from surogates.harness.expert_routing import classify_hard_task

        result = classify_hard_task("Thanks, that helps")

        assert result.required is False
        assert result.category is None


class TestDeadHelpersRemoved:
    """Auto-router helpers must be gone — the design dropped auto-routing.

    See ``docs/superpowers/specs/2026-05-23-expert-mechanism-resurrection-design.md``.
    These assertions prevent the helpers from creeping back via copy-paste.
    """

    def test_select_expert_for_task_removed(self):
        from surogates.harness import expert_routing

        assert not hasattr(expert_routing, "select_expert_for_task")

    def test_load_skills_for_expert_routing_removed(self):
        from surogates.harness import expert_routing

        assert not hasattr(expert_routing, "load_skills_for_expert_routing")

    def test_classify_tool_calls_removed(self):
        from surogates.harness import expert_routing

        assert not hasattr(expert_routing, "classify_tool_calls")

    def test_trigger_helpers_removed(self):
        from surogates.harness import expert_routing

        assert not hasattr(expert_routing, "_normalise_trigger_text")
        assert not hasattr(expert_routing, "_trigger_match_score")


class TestHarnessAdvisorPreflight:
    @pytest.mark.asyncio
    async def test_hard_task_injects_advisor_guidance(self):
        harness = _harness()
        session = _session()
        messages = [{"role": "user", "content": "Write a Python function to parse CSV"}]
        events = [
            Event(id=1, session_id=session.id, type=EventType.USER_MESSAGE.value, data={"content": messages[0]["content"]}),
        ]
        harness._advisor_client.chat.completions.create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="Use csv.DictReader."),
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=11, completion_tokens=4),
                model="advisor-model",
            )
        )

        consulted = await harness._maybe_consult_required_advisor(
            session, messages, events, "system prompt",
        )

        assert consulted is True
        assert messages[-1]["role"] == "user"
        assert "[Advisor guidance: coding]" in messages[-1]["content"]
        assert "Use csv.DictReader." in messages[-1]["content"]
        harness._store.emit_event.assert_any_await(
            session.id,
            EventType.ADVISOR_RESULT,
            {
                "model": "advisor-model",
                "reason": "early",
                "category": "coding",
                "content": "Use csv.DictReader.",
                "input_tokens": 11,
                "output_tokens": 4,
            },
        )

    @pytest.mark.asyncio
    async def test_recovery_skips_duplicate_advisor_guidance(self):
        harness = _harness()
        session = _session()
        messages = [{"role": "user", "content": "Write a Python function"}]
        events = [
            Event(id=1, session_id=session.id, type=EventType.USER_MESSAGE.value, data={"content": messages[0]["content"]}),
            Event(
                id=2,
                session_id=session.id,
                type=EventType.ADVISOR_RESULT.value,
                data={"model": "advisor-model", "reason": "early", "category": "coding"},
            ),
        ]

        consulted = await harness._maybe_consult_required_advisor(
            session, messages, events, "system prompt",
        )

        assert consulted is False
        harness._advisor_client.chat.completions.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_recovery_skips_duplicate_advisor_failure(self):
        harness = _harness()
        session = _session()
        messages = [{"role": "user", "content": "Solve 3x + 7 = 22"}]
        events = [
            Event(id=1, session_id=session.id, type=EventType.USER_MESSAGE.value, data={"content": messages[0]["content"]}),
            Event(
                id=2,
                session_id=session.id,
                type=EventType.ADVISOR_FAILURE.value,
                data={"model": "advisor-model", "reason": "early", "category": "math"},
            ),
        ]

        consulted = await harness._maybe_consult_required_advisor(
            session, messages, events, "system prompt",
        )

        assert consulted is False
        harness._advisor_client.chat.completions.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_advisor_failure_allows_default_model(self):
        harness = _harness()
        session = _session()
        messages = [{"role": "user", "content": "Solve 3x + 7 = 22"}]
        events = [
            Event(id=1, session_id=session.id, type=EventType.USER_MESSAGE.value, data={"content": messages[0]["content"]}),
        ]
        harness._advisor_client.chat.completions.create = AsyncMock(
            side_effect=RuntimeError("advisor unavailable")
        )

        consulted = await harness._maybe_consult_required_advisor(
            session, messages, events, "system prompt",
        )

        assert consulted is False
        assert len(messages) == 1
        harness._store.emit_event.assert_any_await(
            session.id,
            EventType.ADVISOR_FAILURE,
            {
                "model": "advisor-model",
                "reason": "early",
                "category": "math",
                "error": "advisor unavailable",
            },
        )

    def test_harness_has_no_hard_tool_advisor_hook(self):
        from surogates.harness.loop import AgentHarness

        assert not hasattr(AgentHarness, "_maybe_consult_for_tool_calls")
