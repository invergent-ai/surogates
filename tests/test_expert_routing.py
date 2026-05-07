"""Tests for harness-enforced expert routing."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from surogates.harness.budget import IterationBudget
from surogates.harness.loop import AgentHarness
from surogates.session.events import EventType
from surogates.session.models import Event, Session
from surogates.tools.loader import SkillDef
from surogates.tools.registry import ToolRegistry


def _expert(
    name: str,
    trigger: str,
    *,
    status: str = "active",
) -> SkillDef:
    return SkillDef(
        name=name,
        description=f"{name} expert",
        content="Expert instructions.",
        source="org",
        type="expert",
        expert_status=status,
        expert_endpoint="http://expert:8000/v1",
        expert_model="expert-model",
        trigger=trigger,
    )


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

    def test_routes_terminal_tool_call(self):
        from surogates.harness.expert_routing import classify_tool_calls

        result = classify_tool_calls([
            {"function": {"name": "terminal", "arguments": '{"cmd": "pytest"}'}},
        ])

        assert result.required is True
        assert result.category == "terminal"

    def test_routes_patch_tool_call(self):
        from surogates.harness.expert_routing import classify_tool_calls

        result = classify_tool_calls([
            {"function": {"name": "patch", "arguments": "{}"}},
        ])

        assert result.required is True
        assert result.category == "coding"


class TestExpertSelection:
    def test_selects_active_expert_by_trigger(self):
        from surogates.harness.expert_routing import select_expert_for_task

        selected = select_expert_for_task(
            [
                _expert("math_expert", "math, equations"),
                _expert("code_expert", "python, coding, debugging"),
            ],
            "Fix this Python traceback in app.py",
        )

        assert selected is not None
        assert selected.name == "code_expert"

    def test_tie_breaks_by_name(self):
        from surogates.harness.expert_routing import select_expert_for_task

        selected = select_expert_for_task(
            [_expert("z_code", "python"), _expert("a_code", "python")],
            "Write Python code",
        )

        assert selected is not None
        assert selected.name == "a_code"

    def test_ignores_inactive_experts(self):
        from surogates.harness.expert_routing import select_expert_for_task

        selected = select_expert_for_task(
            [_expert("code_expert", "coding", status="draft")],
            "Write Python code",
        )

        assert selected is None

    def test_ignores_active_experts_without_matching_trigger(self):
        from surogates.harness.expert_routing import select_expert_for_task

        selected = select_expert_for_task(
            [_expert("code_expert", "python, coding")],
            "Solve 3x + 7 = 22",
        )

        assert selected is None


class TestHarnessExpertPreflight:
    @pytest.mark.asyncio
    async def test_hard_task_injects_forced_expert_result(self):
        from surogates.tools.builtin.expert_service import ExpertConsultationResult

        harness = _harness()
        session = _session()
        messages = [{"role": "user", "content": "Write a Python function to parse CSV"}]
        events = [
            Event(id=1, session_id=session.id, type=EventType.USER_MESSAGE.value, data={"content": messages[0]["content"]}),
        ]

        service = MagicMock()
        service.consult = AsyncMock(
            return_value=ExpertConsultationResult(
                expert="code_expert",
                success=True,
                content="Use csv.DictReader.",
                iterations_used=1,
            )
        )

        with (
            patch(
                "surogates.harness.loop.load_skills_for_expert_routing",
                AsyncMock(return_value=[_expert("code_expert", "python, coding")]),
            ),
            patch(
                "surogates.harness.loop.ExpertConsultationService",
                return_value=service,
            ),
        ):
            consulted = await harness._maybe_consult_required_expert(
                session, messages, events,
            )

        assert consulted is True
        assert messages[-1]["role"] == "user"
        assert "[Expert consultation: coding via code_expert]" in messages[-1]["content"]
        assert "Use csv.DictReader." in messages[-1]["content"]
        service.consult.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_recovery_skips_duplicate_forced_consultation(self):
        harness = _harness()
        session = _session()
        messages = [{"role": "user", "content": "Write a Python function"}]
        events = [
            Event(id=1, session_id=session.id, type=EventType.USER_MESSAGE.value, data={"content": messages[0]["content"]}),
            Event(
                id=2,
                session_id=session.id,
                type=EventType.EXPERT_DELEGATION.value,
                data={"expert": "code_expert", "forced": True, "category": "coding"},
            ),
        ]

        with patch(
                "surogates.harness.loop.load_skills_for_expert_routing",
                AsyncMock(return_value=[_expert("code_expert", "python, coding")]),
        ) as load_skills:
            consulted = await harness._maybe_consult_required_expert(
                session, messages, events,
            )

        assert consulted is False
        load_skills.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_recovery_skips_duplicate_forced_failure(self):
        harness = _harness()
        session = _session()
        messages = [{"role": "user", "content": "Solve 3x + 7 = 22"}]
        events = [
            Event(id=1, session_id=session.id, type=EventType.USER_MESSAGE.value, data={"content": messages[0]["content"]}),
            Event(
                id=2,
                session_id=session.id,
                type=EventType.EXPERT_FAILURE.value,
                data={"expert": "", "forced": True, "category": "math"},
            ),
        ]

        with patch(
            "surogates.harness.loop.load_skills_for_expert_routing",
            AsyncMock(return_value=[]),
        ) as load_skills:
            consulted = await harness._maybe_consult_required_expert(
                session, messages, events,
            )

        assert consulted is False
        load_skills.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_matching_expert_allows_default_model(self):
        harness = _harness()
        session = _session()
        messages = [{"role": "user", "content": "Solve 3x + 7 = 22"}]
        events = [
            Event(id=1, session_id=session.id, type=EventType.USER_MESSAGE.value, data={"content": messages[0]["content"]}),
        ]

        with patch(
                "surogates.harness.loop.load_skills_for_expert_routing",
                AsyncMock(return_value=[_expert("code_expert", "python, coding")]),
        ):
            consulted = await harness._maybe_consult_required_expert(
                session, messages, events,
            )

        assert consulted is False
        assert len(messages) == 1

    @pytest.mark.asyncio
    async def test_tool_call_intent_consults_expert_before_execution(self):
        from surogates.tools.builtin.expert_service import ExpertConsultationResult

        harness = _harness()
        session = _session()
        messages = [{"role": "user", "content": "Check whether the project is healthy"}]
        events = [
            Event(id=1, session_id=session.id, type=EventType.USER_MESSAGE.value, data={"content": messages[0]["content"]}),
        ]
        tool_calls = [
            {
                "id": "tc_1",
                "function": {"name": "terminal", "arguments": '{"cmd": "pytest"}'},
            },
        ]

        service = MagicMock()
        service.consult = AsyncMock(
            return_value=ExpertConsultationResult(
                expert="terminal_expert",
                success=True,
                content="Run the targeted tests first.",
                iterations_used=1,
            )
        )
        consulted_categories: set[str] = set()

        with (
            patch(
                "surogates.harness.loop.load_skills_for_expert_routing",
                AsyncMock(return_value=[_expert("terminal_expert", "terminal, shell, pytest")]),
            ),
            patch(
                "surogates.harness.loop.ExpertConsultationService",
                return_value=service,
            ),
        ):
            intercepted = await harness._maybe_consult_for_tool_calls(
                session,
                messages,
                events,
                tool_calls,
                consulted_categories,
            )

        assert intercepted is True
        assert "terminal" in consulted_categories
        assert "[Expert consultation: terminal via terminal_expert]" in messages[-1]["content"]
        assert "Run the targeted tests first." in messages[-1]["content"]
        service.consult.assert_awaited_once()
