"""Functional expert consultation, catalog lookup, and expert-loop behavior."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.session.events import EventType
from surogates.tools.loader import (
    SkillDef,
)


class TestSkillsListHidesExperts:
    """``skills_list`` is a catalog of skills, and an expert is not one.

    An expert's SKILL.md is that model's system prompt. Listing experts
    here invited the executor to ``skill_view`` one and do the work
    itself on the cheap model -- indistinguishable from a successful
    consult. Experts are reachable through "Available Experts" +
    ``consult_expert``, and ``/<expert>`` for the human.
    """

    @pytest.mark.asyncio
    async def test_handler_omits_experts_active_or_not(self, monkeypatch):
        from surogates.tools.builtin.skills import _skills_list_handler
        from surogates.tools.loader import (
            EXPERT_STATUS_ACTIVE, EXPERT_STATUS_DRAFT, SkillDef,
        )

        def _expert(name, status):
            return SkillDef(
                name=name, description="d", content="c", source="org_db",
                type="expert", expert_status=status,
            )

        skills = [
            SkillDef(name="code_review", description="d", content="c",
                     source="org_db"),
            _expert("sql_writer", EXPERT_STATUS_ACTIVE),
            _expert("half_baked", EXPERT_STATUS_DRAFT),
        ]

        async def fake_loader(tenant, **kwargs):
            return skills

        monkeypatch.setattr(
            "surogates.tools.builtin.skills._load_all_skills", fake_loader,
        )

        out = json.loads(await _skills_list_handler(
            {}, tenant=SimpleNamespace(org_id=uuid4()),
        ))
        names = [s["name"] for s in out["skills"]]
        assert names == ["code_review"]


class TestConsultExpertHandler:
    """Tests for the consult_expert tool handler."""

    @pytest.fixture
    def active_expert(self) -> SkillDef:
        return SkillDef(
            name="sql_writer",
            description="Writes SQL",
            content="Expert SQL instructions.",
            source="org",
            type="expert",
            expert_model="qwen2.5-coder-7b",
            expert_endpoint="http://expert:8000/v1",
            expert_tools=["terminal"],
            expert_max_iterations=5,
            expert_status="active",
        )

    @pytest.mark.asyncio
    async def test_missing_expert_name(self):
        from surogates.tools.builtin.expert import _consult_expert_handler

        result = await _consult_expert_handler(
            {"task": "do something"},
            tenant=MagicMock(),
            session_id="00000000-0000-0000-0000-000000000001",
            tool_router=MagicMock(),
            tool_registry=MagicMock(),
        )
        data = json.loads(result)
        assert "error" in data
        assert "expert name" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_missing_task(self):
        from surogates.tools.builtin.expert import _consult_expert_handler

        result = await _consult_expert_handler(
            {"expert": "sql_writer"},
            tenant=MagicMock(),
            session_id="00000000-0000-0000-0000-000000000001",
            tool_router=MagicMock(),
            tool_registry=MagicMock(),
        )
        data = json.loads(result)
        assert "error" in data
        assert "task" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_expert_not_found(self):
        from surogates.tools.builtin.expert import _consult_expert_handler

        result = await _consult_expert_handler(
            {"expert": "nonexistent", "task": "do something"},
            tenant=MagicMock(),
            session_id="00000000-0000-0000-0000-000000000001",
            tool_router=MagicMock(),
            tool_registry=MagicMock(),
            loaded_skills=[],
            session_store=AsyncMock(),
        )
        data = json.loads(result)
        assert "error" in data
        assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_resolves_expert_via_api_client_when_bundle_blind(self, monkeypatch):
        """Shared-runtime regression: a per-agent *bundle* expert is invisible to
        the worker-local loader, so consult_expert must resolve it through the
        bundle-aware ``api_client`` (the same path skills_list already uses)."""
        from surogates.tools.builtin import expert as expert_mod
        from surogates.tools.builtin.expert import _consult_expert_handler
        from surogates.tools.builtin.expert_service import ExpertConsultationResult

        class FakeAPIClient:
            async def get_skill(self, name):
                assert name == "ytdclassifier"
                return {
                    "name": "ytdclassifier",
                    "description": "Classifies YTD requests",
                    "content": "Expert instructions.",
                    "type": "expert",
                    "source": "platform",
                    "expert_status": "active",
                    "expert_model": "surogate/Qwen3.5-2B-Libra-YTD",
                    "expert_endpoint": "http://expert:8000/v1",
                    "expert_tools": ["read_file"],
                    "expert_max_iterations": 8,
                }

        captured: dict = {}

        class FakeService:
            def __init__(self, **kwargs):
                captured["init"] = kwargs

            async def consult(self, *, expert, task, context=None, client=None):
                captured["expert"] = expert
                captured["task"] = task
                return ExpertConsultationResult(
                    expert=expert.name, success=True, content='{"ok": true}',
                )

        monkeypatch.setattr(expert_mod, "ExpertConsultationService", FakeService)

        result = await _consult_expert_handler(
            {"expert": "ytdclassifier", "task": "classify this"},
            tenant=MagicMock(),
            session_id="00000000-0000-0000-0000-000000000001",
            tool_registry=MagicMock(),
            session_store=AsyncMock(),
            api_client=FakeAPIClient(),
            loaded_skills=[],  # worker-local loader is bundle-blind
        )

        assert result == '{"ok": true}'
        expert = captured["expert"]
        assert expert.name == "ytdclassifier"
        assert expert.is_active_expert is True
        assert expert.expert_endpoint == "http://expert:8000/v1"
        assert expert.expert_tools == ["read_file"]
        assert expert.expert_max_iterations == 8

    @pytest.mark.asyncio
    async def test_api_client_not_found_lists_available_experts(self, monkeypatch):
        """When the named expert is missing, available_experts comes from the
        bundle-aware catalog, not the empty worker-local list."""
        from surogates.tools.builtin.expert import _consult_expert_handler

        class FakeAPIClient:
            async def get_skill(self, name):
                return None  # not in the catalog

            async def list_skills(self, category=None):
                return json.dumps({
                    "success": True,
                    "skills": [
                        {"name": "ytdclassifier", "type": "expert",
                         "expert_status": "active"},
                        {"name": "draft_one", "type": "expert",
                         "expert_status": "draft"},
                        {"name": "xlsx", "type": "skill"},
                    ],
                })

        result = await _consult_expert_handler(
            {"expert": "nonexistent", "task": "do it"},
            tenant=MagicMock(),
            session_id="00000000-0000-0000-0000-000000000001",
            tool_registry=MagicMock(),
            session_store=AsyncMock(),
            api_client=FakeAPIClient(),
            loaded_skills=[],
        )
        data = json.loads(result)
        assert "not found" in data["error"].lower()
        # Only the active expert is surfaced as available.
        assert data["available_experts"] == ["ytdclassifier"]

    @pytest.mark.asyncio
    async def test_expert_no_endpoint(self, active_expert: SkillDef):
        from surogates.tools.builtin.expert import _consult_expert_handler

        # Clear the endpoint.
        no_endpoint = SkillDef(
            name="sql_writer", description="Writes SQL", content="body",
            source="org", type="expert", expert_status="active",
            expert_endpoint=None,
        )
        result = await _consult_expert_handler(
            {"expert": "sql_writer", "task": "write a query"},
            tenant=MagicMock(),
            session_id="00000000-0000-0000-0000-000000000001",
            tool_router=MagicMock(),
            tool_registry=MagicMock(),
            loaded_skills=[no_endpoint],
            session_store=AsyncMock(),
        )
        data = json.loads(result)
        assert "error" in data
        assert "endpoint" in data["error"].lower()


class TestExpertLoop:
    """Tests for the expert mini agent loop."""

    @pytest.fixture
    def expert(self) -> SkillDef:
        return SkillDef(
            name="test_expert",
            description="Test expert",
            content="Expert instructions.",
            source="org",
            type="expert",
            expert_model="test-model",
            expert_endpoint="http://expert:8000/v1",
            expert_tools=["terminal"],
            expert_max_iterations=3,
            expert_status="active",
        )


    @pytest.mark.asyncio
    async def test_run_expert_loop_applies_generation_params(self, monkeypatch):
        from surogates.tools.builtin import expert_loop as el

        captured: dict = {}

        class _FakeCompletions:
            async def create(self, **kwargs):
                captured.update(kwargs)
                msg = SimpleNamespace(content="done", tool_calls=None)
                return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

        class _FakeClient:
            def __init__(self, *, base_url, api_key):
                self.chat = SimpleNamespace(completions=_FakeCompletions())
            async def close(self):
                pass

        monkeypatch.setattr("openai.AsyncOpenAI", _FakeClient)
        monkeypatch.setattr(
            "surogates.config.load_settings",
            lambda: SimpleNamespace(platform_api_url="http://srv:8888"),
        )

        expert = SkillDef(
            name="ytd", description="c", content="b", source="org",
            type="expert", expert_status="active",
            expert_model="m", expert_endpoint="http://e:8000/v1",
            expert_generation={"temperature": 0, "top_k": 40},
        )
        result, _ = await el.run_expert_loop(
            expert=expert, task="t", context=None,
            tool_router=MagicMock(), tool_registry=MagicMock(),
            tenant=SimpleNamespace(org_config={}), session_id=uuid4(),
        )
        assert result == "done"
        assert captured["temperature"] == 0
        assert captured["extra_body"] == {"top_k": 40}


    @pytest.mark.asyncio
    async def test_run_expert_loop_builds_client_with_absolute_endpoint(self, monkeypatch):
        """End-to-end: a relative expert endpoint becomes an absolute base_url."""
        from surogates.tools.builtin import expert_loop as el

        captured: dict = {}

        class _FakeCompletions:
            async def create(self, **_kwargs):
                msg = SimpleNamespace(content="done", tool_calls=None)
                return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

        class _FakeClient:
            def __init__(self, *, base_url, api_key):
                captured["base_url"] = base_url
                self.chat = SimpleNamespace(completions=_FakeCompletions())

            async def close(self):
                pass

        monkeypatch.setattr("openai.AsyncOpenAI", _FakeClient)
        # The dstack service proxy path is served by the platform SERVER
        # (platform_api_url), NOT the LLM proxy (llm.base_url).  The relative
        # endpoint must resolve against the server origin.
        monkeypatch.setattr(
            "surogates.config.load_settings",
            lambda: SimpleNamespace(
                platform_api_url="http://surogate-server.surogate.svc:8888",
                llm=SimpleNamespace(base_url="http://surogate-proxy.surogate.svc:8889/v1"),
            ),
        )

        expert = SkillDef(
            name="ytd", description="Classifies YTD", content="body",
            source="org", type="expert", expert_status="active",
            expert_model="qwen3-5-2b-libra-ytd-8fd2",
            expert_endpoint="/proxy/services/default/r6b689116/v1",
        )

        result, iterations = await el.run_expert_loop(
            expert=expert, task="classify", context=None,
            tool_router=MagicMock(), tool_registry=MagicMock(),
            tenant=SimpleNamespace(org_config={}), session_id=uuid4(),
        )

        assert result == "done"
        assert iterations == 1
        assert captured["base_url"] == (
            "http://surogate-server.surogate.svc:8888/proxy/services/default/r6b689116/v1"
        )


class TestExpertServiceDelegationEvents:
    """ExpertConsultationService emits delegation before any outcome."""

    @pytest.mark.asyncio
    async def test_missing_endpoint_still_emits_delegation_then_failure(self):
        from surogates.tools.builtin.expert_service import ExpertConsultationService

        store = AsyncMock()
        expert = SkillDef(
            name="sql_writer",
            description="Writes SQL",
            content="body",
            source="org",
            type="expert",
            expert_status="active",
            expert_endpoint=None,
        )
        service = ExpertConsultationService(
            tenant=SimpleNamespace(org_id=uuid4(), user_id=uuid4(), org_config={}),
            session_id=uuid4(),
            tool_registry=MagicMock(),
            session_store=store,
        )

        result = await service.consult(expert=expert, task="write a query")

        assert result.success is False
        emitted_types = [call.args[1] for call in store.emit_event.await_args_list]
        assert emitted_types == [
            EventType.EXPERT_DELEGATION,
            EventType.EXPERT_FAILURE,
        ]
