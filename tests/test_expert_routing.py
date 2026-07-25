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


class TestHarnessAdvisorPreflight:
    """The consult contract: LLM-verdict-gated, buffered, deduped.

    ``classify_hard_task_async`` is patched to an LLM verdict because
    the advisor no longer consults on regex fallbacks — a keyword net
    that over-fires in English and never fires elsewhere is not a good
    enough signal for a pro-tier call.
    """

    @staticmethod
    def _llm_verdict(category="coding"):
        from surogates.harness.expert_routing import HardTaskClassification

        async def _classify(*_a, **_kw):
            return HardTaskClassification(
                True, category, reason="llm", source="llm",
            )

        return _classify

    @pytest.mark.asyncio
    async def test_hard_task_buffers_advisor_guidance(self, monkeypatch):
        from surogates.harness import loop_advisor

        harness = _harness()
        session = _session()
        monkeypatch.setattr(
            loop_advisor, "classify_hard_task_async", self._llm_verdict(),
        )
        messages = [{"role": "user", "content": "Write a Python function to parse CSV"}]
        events = [
            Event(id=1, session_id=session.id, type=EventType.USER_MESSAGE.value, data={"content": messages[0]["content"]}),
        ]
        harness._advisor_client.chat.completions.create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="Use csv.DictReader."),
                        finish_reason="stop",
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
        # Guidance is BUFFERED, never appended to the live list from the
        # background task — a mid-tool-execution append could split an
        # assistant tool_calls message from its results.
        assert len(messages) == 1
        assert len(harness._pending_advisor_messages) == 1
        pending = harness._pending_advisor_messages[0]
        assert pending["role"] == "user"
        assert pending["_advisor"] is True
        assert "[Advisor guidance: coding]" in pending["content"]
        assert "Use csv.DictReader." in pending["content"]

        # The loop flushes at an iteration boundary.
        flushed = harness._flush_pending_advisor_messages(messages)
        assert flushed is True
        assert messages[-1] is pending
        assert harness._pending_advisor_messages == []

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
    async def test_regex_verdict_does_not_consult(self):
        harness = _harness()
        session = _session()
        # No summary client on the mock harness → the LLM classifier is
        # unavailable → regex fallback → no consult, no pro-tier spend.
        messages = [{"role": "user", "content": "Write a Python function to parse CSV"}]
        events = [
            Event(id=1, session_id=session.id, type=EventType.USER_MESSAGE.value, data={"content": messages[0]["content"]}),
        ]

        consulted = await harness._maybe_consult_required_advisor(
            session, messages, events, "system prompt",
        )

        assert consulted is False
        harness._advisor_client.chat.completions.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_recovery_skips_duplicate_advisor_guidance(self, monkeypatch):
        from surogates.harness import loop_advisor

        harness = _harness()
        session = _session()
        monkeypatch.setattr(
            loop_advisor, "classify_hard_task_async", self._llm_verdict(),
        )
        messages = [{"role": "user", "content": "Write a Python function"}]
        events = [
            Event(id=1, session_id=session.id, type=EventType.USER_MESSAGE.value, data={"content": messages[0]["content"]}),
            Event(
                id=2,
                session_id=session.id,
                type=EventType.ADVISOR_RESULT.value,
                data={"model": "advisor-model", "category": "coding"},
            ),
        ]

        consulted = await harness._maybe_consult_required_advisor(
            session, messages, events, "system prompt",
        )

        assert consulted is False
        harness._advisor_client.chat.completions.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_recovery_skips_duplicate_advisor_failure(self, monkeypatch):
        from surogates.harness import loop_advisor

        harness = _harness()
        session = _session()
        monkeypatch.setattr(
            loop_advisor, "classify_hard_task_async", self._llm_verdict("math"),
        )
        messages = [{"role": "user", "content": "Solve 3x + 7 = 22"}]
        events = [
            Event(id=1, session_id=session.id, type=EventType.USER_MESSAGE.value, data={"content": messages[0]["content"]}),
            Event(
                id=2,
                session_id=session.id,
                type=EventType.ADVISOR_FAILURE.value,
                data={"model": "advisor-model", "category": "math"},
            ),
        ]

        consulted = await harness._maybe_consult_required_advisor(
            session, messages, events, "system prompt",
        )

        assert consulted is False
        harness._advisor_client.chat.completions.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_synthetic_user_events_do_not_reset_dedup(self, monkeypatch):
        """A mission kickoff / nudge is not a new human turn."""
        from surogates.harness import loop_advisor

        harness = _harness()
        session = _session()
        monkeypatch.setattr(
            loop_advisor, "classify_hard_task_async", self._llm_verdict(),
        )
        messages = [{"role": "user", "content": "Write a Python function"}]
        events = [
            Event(id=1, session_id=session.id, type=EventType.USER_MESSAGE.value, data={"content": messages[0]["content"]}),
            Event(
                id=2,
                session_id=session.id,
                type=EventType.ADVISOR_RESULT.value,
                data={"model": "advisor-model", "category": "coding"},
            ),
            Event(
                id=3,
                session_id=session.id,
                type=EventType.USER_MESSAGE.value,
                data={"content": "nudge", "synthetic": "mission_kickoff"},
            ),
        ]

        consulted = await harness._maybe_consult_required_advisor(
            session, messages, events, "system prompt",
        )

        assert consulted is False
        harness._advisor_client.chat.completions.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_advisor_failure_allows_default_model(self, monkeypatch):
        from surogates.harness import loop_advisor

        harness = _harness()
        session = _session()
        monkeypatch.setattr(
            loop_advisor, "classify_hard_task_async", self._llm_verdict("math"),
        )
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
        assert harness._pending_advisor_messages == []
        harness._store.emit_event.assert_any_await(
            session.id,
            EventType.ADVISOR_FAILURE,
            {
                "model": "advisor-model",
                "category": "math",
                "error": "advisor unavailable",
            },
        )

    @pytest.mark.asyncio
    async def test_guidance_is_not_mistaken_for_the_users_message(self, monkeypatch):
        """On a later wake the latest user must be the human, not the advisor."""
        from surogates.harness import loop_advisor

        harness = _harness()
        session = _session()
        seen: dict = {}

        async def _classify(msgs, **_kw):
            from surogates.harness.expert_routing import (
                HardTaskClassification,
                _build_classifier_payload,
            )
            seen["latest_user"], _, _ = _build_classifier_payload(msgs)
            return HardTaskClassification(False)

        monkeypatch.setattr(loop_advisor, "classify_hard_task_async", _classify)
        messages = [
            {"role": "user", "content": "Fix the parser bug"},
            {"role": "assistant", "content": "Looking."},
            {
                "role": "user",
                "content": "[Advisor guidance: debugging]\nCheck the delimiter.",
            },
        ]
        await harness._maybe_consult_required_advisor(
            session, messages, [], "system prompt",
        )
        assert seen["latest_user"] == "Fix the parser bug"

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
