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



class TestClassifierClientInjection:
    """The classifier must use the caller's per-session client when given.

    Its ``settings.llm``-derived fallback depends on ``llm.base_url``
    serving chat completions. In the shared-runtime deployment that URL
    is the proxy root, which serves only ``/proxy/services/...`` — so the
    fallback 404s and the classifier silently degrades to regex. Passing
    the session's summary client (a real, billed per-agent route) is what
    keeps the LLM path working.
    """

    @pytest.mark.asyncio
    async def test_injected_aux_is_used_and_settings_fallback_not_built(
        self, monkeypatch,
    ):
        from surogates.harness import expert_routing
        from surogates.harness.auxiliary_client import AuxiliaryLLM

        expert_routing._classifier_cache._store.clear()

        def _boom(*_a, **_kw):  # pragma: no cover - must not be reached
            raise AssertionError(
                "settings fallback was built despite an injected client",
            )

        monkeypatch.setattr(expert_routing, "build_base_auxiliary_llm", _boom)

        captured: dict = {}

        async def fake_generate(**kwargs):
            captured.update(kwargs)
            return expert_routing.HardTaskJudgment(
                required=True, category="coding",
            )

        monkeypatch.setattr(
            expert_routing, "generate_structured", fake_generate,
        )

        aux = AuxiliaryLLM(client=MagicMock(), model="cheap-summary-model")
        result = await expert_routing.classify_hard_task_async(
            [{"role": "user", "content": "refactor the auth module for aux test"}],
            aux=aux,
        )

        assert result.required is True
        assert captured["model"] == "cheap-summary-model"
        assert captured["llm_client"] is aux.client

    @pytest.mark.asyncio
    async def test_falls_back_to_settings_client_when_none_injected(
        self, monkeypatch,
    ):
        from surogates.harness import expert_routing
        from surogates.harness.auxiliary_client import AuxiliaryLLM

        expert_routing._classifier_cache._store.clear()
        built = MagicMock()
        monkeypatch.setattr(
            expert_routing, "build_base_auxiliary_llm",
            lambda *_a, **_kw: AuxiliaryLLM(client=built, model="base-model"),
        )
        monkeypatch.setattr(expert_routing, "load_settings", lambda: object())

        captured: dict = {}

        async def fake_generate(**kwargs):
            captured.update(kwargs)
            return expert_routing.HardTaskJudgment(required=False, category=None)

        monkeypatch.setattr(
            expert_routing, "generate_structured", fake_generate,
        )

        await expert_routing.classify_hard_task_async(
            [{"role": "user", "content": "hello there fallback test"}],
        )
        assert captured["llm_client"] is built

    @pytest.mark.asyncio
    async def test_request_failure_is_logged_at_warning_not_debug(
        self, monkeypatch, caplog,
    ):
        """A 404 from a misconfigured endpoint must not vanish."""
        import logging

        from surogates.harness import expert_routing
        from surogates.harness.auxiliary_client import AuxiliaryLLM

        expert_routing._classifier_cache._store.clear()

        async def boom(**_kwargs):
            raise RuntimeError("404 page not found")

        monkeypatch.setattr(
            expert_routing, "generate_structured", boom,
        )

        aux = AuxiliaryLLM(client=MagicMock(), model="m")
        with caplog.at_level(logging.WARNING, logger="surogates.harness.expert_routing"):
            result = await expert_routing.classify_hard_task_async(
                [{"role": "user", "content": "do something hard warn test"}],
                aux=aux,
            )
        # Still degrades gracefully...
        assert result is not None
        # ...but no longer silently.
        assert any(
            "falling back to the regex classifier" in r.message
            for r in caplog.records
        )
