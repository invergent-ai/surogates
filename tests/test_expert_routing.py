"""Tests for the hard-task classifier and advisor preflight.

Auto-routing to experts was dropped (see
``docs/superpowers/specs/2026-05-23-expert-mechanism-resurrection-design.md``);
``TestDeadHelpersRemoved`` guards against the deleted helpers
creeping back.  The classifier now drives only the hidden advisor
preflight.
"""

from __future__ import annotations

import json
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
        service_account_id=None,
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

    def test_hard_task_classifier_removed(self):
        """The advisor gate is model-driven; nothing classifies turns."""
        from surogates.harness import expert_routing

        for name in (
            "classify_hard_task",
            "classify_hard_task_async",
            "HardTaskClassification",
            "is_advisor_guidance_message",
        ):
            assert not hasattr(expert_routing, name), name

    def test_trigger_helpers_removed(self):
        from surogates.harness import expert_routing

        assert not hasattr(expert_routing, "_normalise_trigger_text")
        assert not hasattr(expert_routing, "_trigger_match_score")


class TestAdvisorTool:
    """The consult contract: model-driven timing, per-turn budget.

    The harness no longer classifies turns or blocks the first request on
    a verdict -- the executor calls the ``advisor`` tool when it wants a
    second opinion, and the guidance comes back as that tool's result.
    """

    @staticmethod
    def _advisor_reply(harness, content="Use csv.DictReader.", tokens=(11, 4)):
        harness._advisor_client.chat.completions.create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=content),
                        finish_reason="stop",
                    )
                ],
                usage=SimpleNamespace(
                    prompt_tokens=tokens[0], completion_tokens=tokens[1],
                ),
                model="advisor-model",
            )
        )

    @pytest.mark.asyncio
    async def test_consult_returns_guidance_and_emits_result(self):
        harness = _harness()
        session = _session()
        harness._advisor_calls_this_turn = 0
        self._advisor_reply(harness)
        messages = [{"role": "user", "content": "Parse this CSV"}]

        guidance = await harness.consult_advisor(
            session=session,
            messages=messages,
            system_prompt="system prompt",
            category="coding",
            task="parsing a CSV",
        )

        assert guidance == "Use csv.DictReader."
        # Guidance reaches the executor as a tool result, so it must NOT
        # be appended to the live message list -- an append mid-tool-
        # execution could split an assistant tool_calls message from its
        # results.
        assert len(messages) == 1
        harness._store.emit_event.assert_any_await(
            session.id,
            EventType.ADVISOR_RESULT,
            {
                "model": "advisor-model",
                "category": "coding",
                "content": "Use csv.DictReader.",
                "truncated": False,
                "input_tokens": 11,
                "output_tokens": 4,
            },
        )

    @pytest.mark.asyncio
    async def test_budget_counts_calls_not_categories(self):
        """Two consults in the same category are allowed within budget.

        The classifier-era dedup was per category, which would block the
        documented pattern of consulting before committing to an approach
        and again before declaring done.
        """
        harness = _harness()
        session = _session()
        harness._advisor_max_calls_per_turn = 2
        harness._advisor_calls_this_turn = 0
        self._advisor_reply(harness)

        async def _consult():
            return await harness.consult_advisor(
                session=session, messages=[], system_prompt="",
                category="coding", task="same category twice",
            )

        assert await _consult() == "Use csv.DictReader."
        assert await _consult() == "Use csv.DictReader."
        # Third exceeds the per-turn budget.
        assert await _consult() is None
        assert harness._advisor_client.chat.completions.create.await_count == 2

    @pytest.mark.asyncio
    async def test_failure_emits_event_and_returns_none(self):
        harness = _harness()
        session = _session()
        harness._advisor_calls_this_turn = 0
        harness._advisor_client.chat.completions.create = AsyncMock(
            side_effect=RuntimeError("upstream exploded"),
        )

        result = await harness.consult_advisor(
            session=session, messages=[], system_prompt="",
            category="planning", task="anything",
        )

        assert result is None
        harness._store.emit_event.assert_any_await(
            session.id,
            EventType.ADVISOR_FAILURE,
            {
                "model": "advisor-model",
                "category": "planning",
                "error": "upstream exploded",
            },
        )

    @pytest.mark.asyncio
    async def test_handler_reports_unavailable_without_raising(self):
        """A spent budget must read as "carry on", not as a tool error."""
        from surogates.tools.builtin.advisor import _advisor_handler

        async def _no_guidance(**_kw):
            return None

        out = json.loads(await _advisor_handler(
            {"category": "coding", "task": "x"}, advisor_consult=_no_guidance,
        ))
        assert out["status"] == "unavailable"
        assert out["guidance"] is None

        # No advisor plumbed at all (advisor-less session).
        out = json.loads(await _advisor_handler({"category": "coding", "task": "x"}))
        assert "error" in out

    def test_advisor_tool_routes_to_the_harness(self):
        """Unlisted tools default to the sandbox executor and fail there.

        The handler calls back into the loop through ``advisor_consult``,
        which does not exist in a sandbox pod.
        """
        from surogates.tools.router import TOOL_LOCATIONS, ToolLocation

        assert TOOL_LOCATIONS["advisor"] is ToolLocation.HARNESS

    def test_classifier_preflight_is_gone(self):
        from surogates.harness import loop, loop_advisor

        assert not hasattr(loop_advisor.AdvisorMixin, "_maybe_consult_required_advisor")
        assert not hasattr(loop_advisor.AdvisorMixin, "_flush_pending_advisor_messages")
        assert not hasattr(loop, "_ADVISOR_PREFLIGHT_TIMEOUT_SECONDS")

    def test_harness_has_no_hard_tool_advisor_hook(self):
        from surogates.harness.loop import AgentHarness

        assert not hasattr(AgentHarness, "_maybe_consult_for_tool_calls")
