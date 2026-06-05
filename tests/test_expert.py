"""Tests for the Experts feature -- task-specialized models as skills.

Covers:
- SkillDef expert field extensions
- Frontmatter parsing of expert fields
- Expert event types
- Expert tool registration and handler
- Expert mini agent loop
- Expert feedback tracking
- Training data collector
- PromptBuilder expert guidance
- API routes for expert actions
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from surogates.session.events import EventType
from surogates.tools.loader import (
    EXPERT_STATUS_ACTIVE,
    EXPERT_STATUS_COLLECTING,
    EXPERT_STATUS_DRAFT,
    EXPERT_STATUS_RETIRED,
    ResourceLoader,
    SkillDef,
    _parse_skill_frontmatter,
    update_frontmatter_field,
)


# =========================================================================
# SkillDef expert fields
# =========================================================================


class TestSkillDefExpertFields:
    """SkillDef dataclass expert-specific attributes."""

    def test_default_is_regular_skill(self):
        s = SkillDef(
            name="test", description="desc", content="body", source="user",
        )
        assert s.type == "skill"
        assert s.is_expert is False
        assert s.is_active_expert is False
        assert s.expert_model is None
        assert s.expert_endpoint is None
        assert s.expert_tools is None
        assert s.expert_max_iterations == 10
        assert s.expert_status == "draft"

    def test_expert_skill(self):
        s = SkillDef(
            name="sql_writer",
            description="Writes SQL",
            content="body",
            source="org",
            type="expert",
            expert_model="qwen2.5-coder-7b",
            expert_endpoint="http://expert:8000/v1",
            expert_tools=["terminal", "read_file"],
            expert_max_iterations=15,
            expert_status="active",
        )
        assert s.is_expert is True
        assert s.is_active_expert is True
        assert s.expert_model == "qwen2.5-coder-7b"
        assert s.expert_tools == ["terminal", "read_file"]

    def test_is_active_expert_requires_active_status(self):
        s = SkillDef(
            name="test", description="desc", content="body", source="org",
            type="expert", expert_status="draft",
        )
        assert s.is_expert is True
        assert s.is_active_expert is False

    def test_is_active_expert_requires_expert_type(self):
        s = SkillDef(
            name="test", description="desc", content="body", source="org",
            type="skill", expert_status="active",
        )
        assert s.is_expert is False
        assert s.is_active_expert is False

    def test_expert_generation_default_none(self):
        s = SkillDef(name="t", description="d", content="c", source="org")
        assert s.expert_generation is None


# =========================================================================
# Frontmatter parsing of expert fields
# =========================================================================


class TestExpertFrontmatterParsing:
    """Parsing SKILL.md frontmatter with expert fields."""

    def test_parse_expert_type(self):
        text = (
            "---\nname: sql_writer\n"
            "description: Writes SQL\n"
            "type: expert\n"
            "---\nBody content\n"
        )
        parsed = _parse_skill_frontmatter(text, "fallback")
        assert parsed["type"] == "expert"

    def test_parse_expert_model_and_endpoint(self):
        text = (
            "---\nname: sql_writer\n"
            "description: Writes SQL\n"
            "type: expert\n"
            "base_model: qwen2.5-coder-7b\n"
            "endpoint: http://expert:8000/v1\n"
            "adapter: sql_writer/adapter/\n"
            "---\nBody\n"
        )
        parsed = _parse_skill_frontmatter(text, "fallback")
        assert parsed["expert_model"] == "qwen2.5-coder-7b"
        assert parsed["expert_endpoint"] == "http://expert:8000/v1"
        assert parsed["expert_adapter"] == "sql_writer/adapter/"

    def test_parse_expert_model_alias(self):
        text = (
            "---\nname: code_expert\n"
            "description: Handles coding tasks\n"
            "type: expert\n"
            "model: claude-sonnet-4-6\n"
            "---\nBody\n"
        )
        parsed = _parse_skill_frontmatter(text, "fallback")
        assert parsed["expert_model"] == "claude-sonnet-4-6"

    def test_model_field_preferred_over_legacy_base_model(self):
        text = (
            "---\nname: code_expert\n"
            "description: Handles coding tasks\n"
            "type: expert\n"
            "base_model: old-model\n"
            "model: new-model\n"
            "---\nBody\n"
        )
        parsed = _parse_skill_frontmatter(text, "fallback")
        assert parsed["expert_model"] == "new-model"

    def test_parse_expert_trigger_list(self):
        text = (
            "---\nname: code_expert\n"
            "description: Handles coding tasks\n"
            "type: expert\n"
            "trigger: [coding, debugging, terminal commands]\n"
            "---\nBody\n"
        )
        parsed = _parse_skill_frontmatter(text, "fallback")
        assert parsed["trigger"] == "coding, debugging, terminal commands"

    def test_parse_expert_tools_list(self):
        text = (
            "---\nname: test\n"
            "description: desc\n"
            "type: expert\n"
            "tools: [terminal, read_file, search_files]\n"
            "---\nBody\n"
        )
        parsed = _parse_skill_frontmatter(text, "fallback")
        assert parsed["expert_tools"] == ["terminal", "read_file", "search_files"]

    def test_parse_expert_tools_csv_string(self):
        text = (
            "---\nname: test\n"
            "description: desc\n"
            "type: expert\n"
            "tools: terminal, read_file\n"
            "---\nBody\n"
        )
        parsed = _parse_skill_frontmatter(text, "fallback")
        assert parsed["expert_tools"] == ["terminal", "read_file"]

    def test_parse_max_iterations(self):
        text = (
            "---\nname: test\n"
            "description: desc\n"
            "type: expert\n"
            "max_iterations: 20\n"
            "---\nBody\n"
        )
        parsed = _parse_skill_frontmatter(text, "fallback")
        # Native YAML int survives the parse; string form is accepted
        # too by ``_build_skill_def``, which int()-coerces either way.
        assert parsed["expert_max_iterations"] == 20

    def test_parse_expert_status(self):
        text = (
            "---\nname: test\n"
            "description: desc\n"
            "type: expert\n"
            "expert_status: active\n"
            "---\nBody\n"
        )
        parsed = _parse_skill_frontmatter(text, "fallback")
        assert parsed["expert_status"] == "active"

    def test_regular_skill_has_no_expert_fields(self):
        text = (
            "---\nname: regular\n"
            "description: A regular skill\n"
            "---\nBody\n"
        )
        parsed = _parse_skill_frontmatter(text, "fallback")
        assert "type" not in parsed  # Not set for regular skills
        assert "expert_model" not in parsed
        assert "expert_endpoint" not in parsed

    def test_parse_generation_block(self):
        text = (
            "---\nname: ytd\n"
            "description: classifier\n"
            "type: expert\n"
            "generation:\n"
            "  temperature: 0\n"
            "  top_k: 40\n"
            "  repetition_penalty: 1.1\n"
            "---\nBody\n"
        )
        parsed = _parse_skill_frontmatter(text, "fallback")
        assert parsed["expert_generation"] == {
            "temperature": 0, "top_k": 40, "repetition_penalty": 1.1,
        }


# =========================================================================
# ResourceLoader with expert skills
# =========================================================================


class TestResourceLoaderExperts:
    """ResourceLoader correctly loads expert skills from the filesystem."""

    def test_load_expert_from_dir(self, tmp_path: Path):
        skills_dir = tmp_path / "skills"
        expert_dir = skills_dir / "sql_writer"
        expert_dir.mkdir(parents=True)
        (expert_dir / "SKILL.md").write_text(
            "---\n"
            "name: sql_writer\n"
            "description: Writes SQL queries\n"
            "type: expert\n"
            "base_model: qwen2.5-coder-7b\n"
            "endpoint: http://expert:8000/v1\n"
            "tools: [terminal, read_file]\n"
            "max_iterations: 15\n"
            "expert_status: active\n"
            "---\n"
            "Expert instructions here.\n",
            encoding="utf-8",
        )

        loader = ResourceLoader(
            platform_skills_dir=str(skills_dir),
            platform_mcp_dir=str(tmp_path / "mcp"),
        )
        skills = loader._load_skills_from_dir(str(skills_dir), "platform")

        assert len(skills) == 1
        s = skills[0]
        assert s.name == "sql_writer"
        assert s.type == "expert"
        assert s.is_expert is True
        assert s.is_active_expert is True
        assert s.expert_model == "qwen2.5-coder-7b"
        assert s.expert_endpoint == "http://expert:8000/v1"
        assert s.expert_tools == ["terminal", "read_file"]
        assert s.expert_max_iterations == 15
        assert s.expert_status == "active"
        assert "Expert instructions here." in s.content

    def test_mixed_skills_and_experts(self, tmp_path: Path):
        skills_dir = tmp_path / "skills"

        # Regular skill.
        regular_dir = skills_dir / "code-review"
        regular_dir.mkdir(parents=True)
        (regular_dir / "SKILL.md").write_text(
            "---\nname: code-review\ndescription: Reviews code\n---\nReview instructions.\n",
            encoding="utf-8",
        )

        # Expert skill.
        expert_dir = skills_dir / "sql-expert"
        expert_dir.mkdir(parents=True)
        (expert_dir / "SKILL.md").write_text(
            "---\nname: sql-expert\ndescription: SQL expert\n"
            "type: expert\nbase_model: phi-3\n"
            "endpoint: http://e:8000/v1\n"
            "expert_status: active\n---\nSQL body.\n",
            encoding="utf-8",
        )

        loader = ResourceLoader(
            platform_skills_dir=str(skills_dir),
            platform_mcp_dir=str(tmp_path / "mcp"),
        )
        skills = loader._load_skills_from_dir(str(skills_dir), "org")

        assert len(skills) == 2
        types = {s.name: s.type for s in skills}
        assert types["code-review"] == "skill"
        assert types["sql-expert"] == "expert"


# =========================================================================
# Event types
# =========================================================================


class TestExpertEventTypes:
    """Expert-related event types exist in the EventType enum."""

    def test_expert_delegation_exists(self):
        assert EventType.EXPERT_DELEGATION.value == "expert.delegation"

    def test_expert_result_exists(self):
        assert EventType.EXPERT_RESULT.value == "expert.result"

    def test_expert_failure_exists(self):
        assert EventType.EXPERT_FAILURE.value == "expert.failure"

    def test_expert_override_exists(self):
        assert EventType.EXPERT_OVERRIDE.value == "expert.override"


# =========================================================================
# Tool registration
# =========================================================================


class TestExpertToolRegistration:
    """The consult_expert tool registers correctly."""

    def test_register(self):
        from surogates.tools.builtin.expert import register
        from surogates.tools.registry import ToolRegistry

        reg = ToolRegistry()
        register(reg)
        assert reg.has("consult_expert")
        entry = reg.get("consult_expert")
        assert entry.toolset == "expert"
        assert entry.schema.name == "consult_expert"

    def test_schema_has_required_params(self):
        from surogates.tools.builtin.expert import _EXPERT_SCHEMA

        params = _EXPERT_SCHEMA.parameters
        assert "expert" in params["properties"]
        assert "task" in params["properties"]
        assert "context" in params["properties"]
        assert params["required"] == ["expert", "task"]


class TestConsultExpertSchemaDescription:
    """consult_expert description must not collide with delegate_task vocabulary."""

    def test_description_uses_consult_not_delegate(self):
        from surogates.tools.builtin.expert import _EXPERT_SCHEMA

        desc = _EXPERT_SCHEMA.description.lower()
        assert "consult" in desc
        # Must not use the words that belong to delegate_task.
        assert "delegate" not in desc
        assert "subtask" not in desc
        assert "sub-task" not in desc

    def test_description_mentions_specialist_and_specialty(self):
        from surogates.tools.builtin.expert import _EXPERT_SCHEMA

        desc = _EXPERT_SCHEMA.description.lower()
        assert "specialist" in desc
        assert "specialty" in desc


class TestSkillsListExpertMetadata:
    """skills_list returns enough metadata to identify and address active experts."""

    @pytest.mark.asyncio
    async def test_handler_includes_expert_fields(self, monkeypatch):
        import json
        from types import SimpleNamespace
        from uuid import uuid4

        from surogates.tools.builtin.skills import _skills_list_handler
        from surogates.tools.loader import SkillDef

        active = SkillDef(
            name="sql_writer",
            description="Writes SQL",
            content="body",
            source="org",
            type="expert",
            expert_status="active",
            expert_model="qwen2.5-coder-7b",
            expert_endpoint="http://expert:8000/v1",
            trigger="SQL queries, database schemas",
        )
        plain = SkillDef(
            name="code_review",
            description="Reviews code",
            content="body",
            source="org",
            type="skill",
            trigger="code review",
        )

        async def fake_loader(tenant, **kwargs):
            return [active, plain]

        monkeypatch.setattr(
            "surogates.tools.builtin.skills._load_all_skills", fake_loader,
        )

        tenant = SimpleNamespace(org_id=uuid4())
        out = await _skills_list_handler({}, tenant=tenant)
        payload = json.loads(out)

        by_name = {s["name"]: s for s in payload["skills"]}
        assert by_name["sql_writer"]["type"] == "expert"
        assert by_name["sql_writer"]["trigger"] == "SQL queries, database schemas"
        assert by_name["sql_writer"]["expert_status"] == "active"
        assert by_name["sql_writer"]["expert_model"] == "qwen2.5-coder-7b"
        assert by_name["sql_writer"]["expert_endpoint"] == "http://expert:8000/v1"
        assert by_name["code_review"]["type"] == "skill"
        # Regular skills do not get expert_* keys.
        assert "expert_status" not in by_name["code_review"]
        assert "expert_model" not in by_name["code_review"]

    def test_schema_description_directs_to_consult_expert(self):
        from surogates.tools.builtin.skills import SKILLS_LIST_SCHEMA

        desc = SKILLS_LIST_SCHEMA.description
        assert "type: expert" in desc or "type=expert" in desc.replace(": ", "=")
        assert "consult_expert" in desc

    @pytest.mark.asyncio
    async def test_handler_hides_inactive_experts(self, monkeypatch):
        """draft / collecting / retired experts must not appear in the catalog.

        The slash dispatcher only routes active experts to the
        mini-loop; inactive ones would fall through to ``skill_view``
        and inline their system prompt as a skill body, which is the
        wrong UX.  The ``# Available Experts`` system-prompt section
        and ``consult_expert``'s active-expert resolver already enforce
        the same invariant; this test keeps the catalog tool aligned.
        """
        import json
        from types import SimpleNamespace
        from uuid import uuid4

        from surogates.tools.builtin.skills import _skills_list_handler
        from surogates.tools.loader import SkillDef

        active = SkillDef(
            name="sql_writer",
            description="Writes SQL",
            content="body",
            source="org",
            type="expert",
            expert_status="active",
            expert_endpoint="http://expert:8000/v1",
        )
        draft = SkillDef(
            name="draft_specialist",
            description="Not yet active",
            content="body",
            source="org",
            type="expert",
            expert_status="draft",
        )
        retired = SkillDef(
            name="retired_specialist",
            description="Decommissioned",
            content="body",
            source="org",
            type="expert",
            expert_status="retired",
        )
        regular = SkillDef(
            name="code_review",
            description="Reviews code",
            content="body",
            source="org",
            type="skill",
        )

        async def fake_loader(tenant, **kwargs):
            return [active, draft, retired, regular]

        monkeypatch.setattr(
            "surogates.tools.builtin.skills._load_all_skills", fake_loader,
        )

        tenant = SimpleNamespace(org_id=uuid4())
        out = await _skills_list_handler({}, tenant=tenant)
        payload = json.loads(out)

        names = [s["name"] for s in payload["skills"]]
        assert "sql_writer" in names
        assert "code_review" in names
        assert "draft_specialist" not in names
        assert "retired_specialist" not in names
        # `count` mirrors the visible list, not the underlying catalog.
        assert payload["count"] == 2


# =========================================================================
# consult_expert handler
# =========================================================================


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

            async def consult(self, *, expert, task, context=None):
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


# =========================================================================
# Expert mini agent loop
# =========================================================================


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

    def test_expert_budget_exceeded_error(self):
        from surogates.tools.builtin.expert_loop import ExpertBudgetExceeded

        exc = ExpertBudgetExceeded("sql_writer", 10)
        assert "sql_writer" in str(exc)
        assert "10" in str(exc)
        assert exc.expert_name == "sql_writer"
        assert exc.max_iterations == 10

    def test_generation_kwargs_splits_standard_and_extra_body(self):
        from surogates.tools.builtin.expert_loop import _generation_kwargs

        kwargs, extra = _generation_kwargs(
            {"temperature": 0, "top_p": 0.9, "max_tokens": 512,
             "top_k": 40, "repetition_penalty": 1.1}
        )
        assert kwargs == {"temperature": 0, "top_p": 0.9, "max_tokens": 512}
        assert extra == {"top_k": 40, "repetition_penalty": 1.1}

    def test_generation_kwargs_omits_unset(self):
        from surogates.tools.builtin.expert_loop import _generation_kwargs

        kwargs, extra = _generation_kwargs({"temperature": 0.2})
        assert kwargs == {"temperature": 0.2}
        assert extra == {}

    def test_generation_kwargs_none(self):
        from surogates.tools.builtin.expert_loop import _generation_kwargs

        assert _generation_kwargs(None) == ({}, {})

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

    def test_absolute_endpoint_absolutizes_relative_proxy_path(self):
        """A relative ops-proxy endpoint resolves against the worker's LLM origin.

        ``resolve_model_endpoint`` (ops) emits a relative path like
        ``/proxy/services/default/<run>`` for dstack-served expert models, but
        the OpenAI SDK needs an absolute base_url.
        """
        from surogates.tools.builtin.expert_loop import _absolute_endpoint

        out = _absolute_endpoint(
            "/proxy/services/default/r6b689116",
            "http://surogate-proxy.surogate.svc:8889/v1",
        )
        assert out == (
            "http://surogate-proxy.surogate.svc:8889/proxy/services/default/r6b689116"
        )

    def test_absolute_endpoint_passes_through_absolute_url(self):
        from surogates.tools.builtin.expert_loop import _absolute_endpoint

        out = _absolute_endpoint(
            "http://expert:8000/v1", "http://surogate-proxy.surogate.svc:8889/v1",
        )
        assert out == "http://expert:8000/v1"

    def test_absolute_endpoint_returns_relative_when_base_unusable(self):
        from surogates.tools.builtin.expert_loop import _absolute_endpoint

        # No usable origin to resolve against -> leave the path untouched
        # rather than fabricate a wrong URL.
        assert _absolute_endpoint("/proxy/x", "") == "/proxy/x"
        assert _absolute_endpoint("/proxy/x", None) == "/proxy/x"

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

    def test_build_expert_system_prompt(self):
        from surogates.tools.builtin.expert_loop import _build_expert_system_prompt

        expert = SkillDef(
            name="sql", description="Writes SQL", content="Use PostgreSQL syntax.",
            source="org", type="expert", expert_tools=["terminal", "read_file"],
        )
        prompt = _build_expert_system_prompt(expert)
        assert "Writes SQL" in prompt
        assert "terminal" in prompt
        assert "read_file" in prompt
        assert "Use PostgreSQL syntax." in prompt

    def test_build_user_message_with_context(self):
        from surogates.tools.builtin.expert_loop import _build_user_message

        msg = _build_user_message("Write a query", "Schema: users(id, name)")
        assert "Write a query" in msg
        assert "Schema: users(id, name)" in msg
        assert "## Context" in msg

    def test_build_user_message_without_context(self):
        from surogates.tools.builtin.expert_loop import _build_user_message

        msg = _build_user_message("Write a query", None)
        assert msg == "Write a query"

    def test_resolve_api_key_from_org_config(self):
        from surogates.tools.builtin.expert_loop import _resolve_api_key

        tenant = SimpleNamespace(
            org_config={"expert_api_keys": {"sql": "sk-123"}},
        )
        expert = SkillDef(
            name="sql", description="d", content="c", source="org",
            type="expert",
        )
        assert _resolve_api_key(tenant, expert) == "sk-123"

    def test_resolve_api_key_global_fallback(self):
        from surogates.tools.builtin.expert_loop import _resolve_api_key

        tenant = SimpleNamespace(org_config={"expert_api_key": "sk-global"})
        expert = SkillDef(
            name="other", description="d", content="c", source="org",
            type="expert",
        )
        assert _resolve_api_key(tenant, expert) == "sk-global"

    def test_resolve_api_key_default(self):
        from surogates.tools.builtin.expert_loop import _resolve_api_key

        tenant = SimpleNamespace(org_config={})
        expert = SkillDef(
            name="x", description="d", content="c", source="org",
            type="expert",
        )
        assert _resolve_api_key(tenant, expert) == "not-needed"


# =========================================================================
# Expert feedback
# =========================================================================


class TestExpertFeedback:
    """Tests for expert outcome tracking."""

    @pytest.mark.asyncio
    async def test_record_success_emits_event(self):
        from surogates.tools.builtin.expert_feedback import record_expert_outcome

        session_store = AsyncMock()
        await record_expert_outcome(
            session_store=session_store,
            session_id=uuid4(),
            expert_name="sql_writer",
            success=True,
            iterations_used=3,
        )

        session_store.emit_event.assert_called_once()
        call_args = session_store.emit_event.call_args
        assert call_args[0][1] == EventType.EXPERT_RESULT
        assert call_args[0][2]["success"] is True

    @pytest.mark.asyncio
    async def test_record_failure_emits_event(self):
        from surogates.tools.builtin.expert_feedback import record_expert_outcome

        session_store = AsyncMock()
        await record_expert_outcome(
            session_store=session_store,
            session_id=uuid4(),
            expert_name="sql_writer",
            success=False,
            error="budget exceeded",
        )

        session_store.emit_event.assert_called_once()
        call_args = session_store.emit_event.call_args
        assert call_args[0][1] == EventType.EXPERT_FAILURE
        assert call_args[0][2]["error"] == "budget exceeded"

    @pytest.mark.asyncio
    async def test_record_handles_missing_session_store(self):
        from surogates.tools.builtin.expert_feedback import record_expert_outcome

        # Should not raise.
        await record_expert_outcome(
            session_store=None,
            session_id=uuid4(),
            expert_name="sql_writer",
            success=True,
        )


class TestRecordExpertOutcomeSlim:
    """record_expert_outcome only emits events; no DB stat updates."""

    @pytest.mark.asyncio
    async def test_emits_result_event_with_content_on_success(self):
        from surogates.tools.builtin.expert_feedback import record_expert_outcome

        store = AsyncMock()
        session_id = uuid4()
        await record_expert_outcome(
            session_store=store,
            session_id=session_id,
            expert_name="sql_writer",
            success=True,
            iterations_used=3,
            content="SELECT 1",
        )
        store.emit_event.assert_awaited_once()
        args, _ = store.emit_event.call_args
        assert args[0] == session_id
        assert args[1] is EventType.EXPERT_RESULT
        assert args[2]["expert"] == "sql_writer"
        assert args[2]["success"] is True
        assert args[2]["iterations_used"] == 3
        assert args[2]["content"] == "SELECT 1"

    def test_signature_has_no_db_kwargs(self):
        import inspect
        from surogates.tools.builtin.expert_feedback import record_expert_outcome

        params = inspect.signature(record_expert_outcome).parameters
        assert "db_session" not in params
        assert "skill_id" not in params

    def test_auto_disable_constants_removed(self):
        from surogates.tools.builtin import expert_feedback

        assert not hasattr(expert_feedback, "AUTO_DISABLE_THRESHOLD")
        assert not hasattr(expert_feedback, "MIN_USES_FOR_AUTO_DISABLE")
        assert not hasattr(expert_feedback, "_update_db_stats")

    def test_signature_has_no_forced_or_category_kwargs(self):
        """Vestiges of the dropped auto-route path — should not be in the API."""
        import inspect
        from surogates.tools.builtin.expert_feedback import record_expert_outcome
        from surogates.tools.builtin.expert_service import ExpertConsultationService

        outcome_params = inspect.signature(record_expert_outcome).parameters
        assert "forced" not in outcome_params
        assert "category" not in outcome_params

        consult_params = inspect.signature(ExpertConsultationService.consult).parameters
        assert "forced" not in consult_params
        assert "category" not in consult_params

    @pytest.mark.asyncio
    async def test_delegation_event_has_no_forced_or_category_fields(self):
        """The delegation event payload should not carry the dropped fields."""
        from unittest.mock import AsyncMock, MagicMock
        from uuid import uuid4

        from surogates.session.events import EventType
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

        await service.consult(expert=expert, task="write a query")

        for call in store.emit_event.await_args_list:
            _, event_type, data = call.args
            if event_type is EventType.EXPERT_DELEGATION:
                assert "forced" not in data
                assert "category" not in data
                break
        else:  # pragma: no cover -- defensive
            raise AssertionError("expected an EXPERT_DELEGATION event")


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


# =========================================================================
# Training data collector
# =========================================================================


class TestTrainingDataCollector:
    """Tests for the TrainingDataCollector."""

    def test_training_example_to_jsonl(self):
        from surogates.jobs.training_collector import TrainingExample

        ex = TrainingExample(
            messages=[
                {"role": "user", "content": "Write a query"},
                {"role": "assistant", "content": "SELECT * FROM users"},
            ],
            session_id=uuid4(),
            expert_name="sql_writer",
        )
        line = ex.to_jsonl_line()
        data = json.loads(line)
        assert "messages" in data
        assert len(data["messages"]) == 2
        assert data["messages"][0]["role"] == "user"

    def test_training_example_to_jsonl_dict(self):
        from surogates.jobs.training_collector import TrainingExample

        ex = TrainingExample(
            messages=[{"role": "user", "content": "test"}],
            session_id=uuid4(),
            expert_name="test",
        )
        d = ex.to_jsonl_dict()
        assert d == {"messages": [{"role": "user", "content": "test"}]}

    @pytest.mark.asyncio
    async def test_collect_returns_empty_for_no_sessions(self):
        from surogates.jobs.training_collector import TrainingDataCollector

        store = AsyncMock()
        store.list_sessions = AsyncMock(return_value=[])

        collector = TrainingDataCollector(session_store=store)
        examples = await collector.collect_for_expert(
            expert_name="sql_writer",
            org_id=uuid4(),
        )
        assert examples == []

    @pytest.mark.asyncio
    async def test_extract_skips_overridden_experts(self):
        from surogates.jobs.training_collector import TrainingDataCollector

        override_event = SimpleNamespace(
            type=EventType.EXPERT_OVERRIDE.value,
            data={"expert": "sql_writer"},
        )
        store = AsyncMock()
        store.get_events = AsyncMock(return_value=[override_event])

        collector = TrainingDataCollector(session_store=store)
        examples = await collector._extract_from_session(
            session_id=uuid4(),
            expert_name="sql_writer",
        )
        assert examples == []

    @pytest.mark.asyncio
    async def test_export_jsonl_with_no_examples(self):
        from surogates.jobs.training_collector import TrainingDataCollector

        collector = TrainingDataCollector(session_store=AsyncMock())
        key = await collector.export_jsonl("sql_writer", [], uuid4())
        assert key == ""

    @pytest.mark.asyncio
    async def test_export_jsonl_writes_to_storage(self):
        from surogates.jobs.training_collector import TrainingDataCollector, TrainingExample

        storage = AsyncMock()
        collector = TrainingDataCollector(
            session_store=AsyncMock(),
            storage=storage,
        )

        org_id = uuid4()
        examples = [
            TrainingExample(
                messages=[{"role": "user", "content": "test"}],
                session_id=uuid4(),
                expert_name="sql_writer",
            ),
        ]

        key = await collector.export_jsonl("sql_writer", examples, org_id)
        assert "sql_writer" in key
        assert key.endswith(".jsonl")
        storage.write.assert_called_once()
        call_args = storage.write.call_args
        assert f"tenant-{org_id}" == call_args[0][0]
        content = call_args[0][2].decode("utf-8")
        assert '"messages"' in content


# =========================================================================
# PromptBuilder expert guidance
# =========================================================================


class TestPromptBuilderExpertSection:
    """PromptBuilder renders the # Available Experts section for active experts."""

    @pytest.fixture
    def tenant(self):
        return SimpleNamespace(
            org_id=uuid4(),
            user_id=uuid4(),
            org_config={"default_model": "gpt-4o"},
            user_preferences={},
            asset_root="/tmp/test_assets",
        )

    def test_section_empty_when_no_experts(self, tenant):
        from surogates.harness.prompt import PromptBuilder

        pb = PromptBuilder(tenant=tenant, skills=[])
        assert pb._available_experts_section() == ""

    def test_section_lists_active_experts(self, tenant):
        from surogates.harness.prompt import PromptBuilder

        skills = [
            SkillDef(
                name="sql_writer",
                description="Writes PostgreSQL queries from natural language descriptions",
                content="body",
                source="org",
                type="expert",
                expert_status="active",
                trigger="SQL queries, database schemas, PostgreSQL, data analysis",
            ),
            SkillDef(
                name="draft_expert",
                description="Not yet active",
                content="body",
                source="org",
                type="expert",
                expert_status="draft",
                trigger="something",
            ),
            SkillDef(
                name="regular_skill",
                description="A normal skill",
                content="body",
                source="org",
                type="skill",
            ),
        ]

        pb = PromptBuilder(tenant=tenant, skills=skills)
        section = pb._available_experts_section()

        assert "# Available Experts" in section
        assert "sql_writer" in section
        assert "Writes PostgreSQL queries" in section
        assert "Specialty: SQL queries, database schemas" in section
        # Only active experts are listed.
        assert "draft_expert" not in section
        # Regular skills do not appear here.
        assert "regular_skill" not in section
        # Section instructs the LLM how to invoke and disambiguates from delegate_task.
        assert "consult_expert(expert, task)" in section
        assert "delegate_task" in section
        assert "Do NOT use" in section

    def test_section_emitted_in_build_when_active_expert_exists(self, tenant):
        from surogates.harness.prompt import PromptBuilder

        skills = [
            SkillDef(
                name="sql_writer",
                description="SQL specialist",
                content="body",
                source="org",
                type="expert",
                expert_status="active",
                trigger="SQL queries",
            ),
        ]
        pb = PromptBuilder(tenant=tenant, skills=skills)
        prompt = pb.build()
        assert "# Available Experts" in prompt
        assert "sql_writer" in prompt

    def test_section_omitted_in_build_when_no_active_expert(self, tenant):
        from surogates.harness.prompt import PromptBuilder

        skills = [
            SkillDef(
                name="regular_skill",
                description="A normal skill",
                content="body",
                source="org",
                type="skill",
            ),
        ]
        pb = PromptBuilder(tenant=tenant, skills=skills)
        prompt = pb.build()
        assert "# Available Experts" not in prompt

    def test_skills_section_still_excludes_experts(self, tenant):
        """Regression: experts must not bleed into the regular skills catalog."""
        from surogates.harness.prompt import PromptBuilder

        skills = [
            SkillDef(
                name="sql_writer",
                description="SQL specialist",
                content="body",
                source="org",
                type="expert",
                expert_status="active",
                trigger="SQL queries",
            ),
            SkillDef(
                name="code_review",
                description="Reviews code",
                content="body",
                source="org",
                type="skill",
            ),
        ]
        pb = PromptBuilder(tenant=tenant, skills=skills)
        skills_section = pb._skills_section()
        # sql_writer must NOT appear in the regular skills index — that
        # remains the contract of _skills_section.
        assert "sql_writer" not in skills_section
        assert "code_review" in skills_section


class TestWorkerExpertCatalogWiring:
    """Worker prompt setup loads skills so available experts reach PromptBuilder."""

    @pytest.mark.asyncio
    async def test_load_prompt_catalogs_returns_agents_and_skills(self, monkeypatch):
        from surogates.orchestrator import worker as worker_mod

        loaded_agents = [SimpleNamespace(name="assistant")]
        loaded_skills = [
            SkillDef(
                name="code_expert",
                description="Handles code",
                content="body",
                source="org",
                type="expert",
                expert_status=EXPERT_STATUS_ACTIVE,
                trigger="coding",
            )
        ]
        init_kwargs = {}

        class FakeResourceLoader:
            def __init__(self, **kwargs):
                init_kwargs.update(kwargs)

            async def load_agents(self, tenant, db_session=None):
                assert db_session == "db-session"
                return loaded_agents

            async def load_skills(self, tenant, db_session=None):
                assert db_session == "db-session"
                return loaded_skills

        class FakeSessionFactory:
            def __call__(self):
                return self

            async def __aenter__(self):
                return "db-session"

            async def __aexit__(self, exc_type, exc, tb):
                return False

        monkeypatch.setattr(worker_mod, "ResourceLoader", FakeResourceLoader)

        tenant = SimpleNamespace(org_id=uuid4())
        settings = SimpleNamespace(
            platform_agents_dir="/agents",
            platform_skills_dir="/skills",
        )

        agents, skills = await worker_mod._load_prompt_catalogs(
            settings=settings,
            tenant=tenant,
            session_factory=FakeSessionFactory(),
        )

        assert init_kwargs == {
            "platform_agents_dir": "/agents",
            "platform_skills_dir": "/skills",
        }
        assert agents == loaded_agents
        assert skills == loaded_skills


# =========================================================================
# Tool router location
# =========================================================================


class TestToolRouterExpertLocation:
    """consult_expert routes to the harness, not the sandbox."""

    def test_consult_expert_routes_to_harness(self):
        from surogates.tools.router import TOOL_LOCATIONS, ToolLocation

        assert TOOL_LOCATIONS["consult_expert"] is ToolLocation.HARNESS

    def test_consult_expert_resolves_to_harness_via_router(self):
        from unittest.mock import MagicMock
        from surogates.tools.router import ToolLocation, ToolRouter

        router = ToolRouter(
            registry=MagicMock(),
            sandbox_pool=MagicMock(),
            governance=MagicMock(),
        )
        assert router.resolve_location("consult_expert") is ToolLocation.HARNESS


# =========================================================================
# ToolRuntime registers expert
# =========================================================================


class TestToolRuntimeRegistersExpert:
    """ToolRuntime.register_builtins() exposes consult_expert to executors."""

    def test_expert_registered(self):
        from surogates.tools.registry import ToolRegistry
        from surogates.tools.runtime import ToolRuntime

        reg = ToolRegistry()
        runtime = ToolRuntime(reg)
        runtime.register_builtins()
        assert reg.has("consult_expert")
        entry = reg.get("consult_expert")
        assert entry.schema.name == "consult_expert"
        assert entry.toolset == "expert"


# =========================================================================
# get_active_experts helper
# =========================================================================


class TestGetActiveExperts:
    """The get_active_experts utility filters correctly."""

    def test_filters_active_experts(self):
        from surogates.tools.builtin.expert import get_active_experts

        skills = [
            SkillDef(name="a", description="d", content="c", source="org", type="skill"),
            SkillDef(
                name="b", description="d", content="c", source="org",
                type="expert", expert_status="active",
            ),
            SkillDef(
                name="c", description="d", content="c", source="org",
                type="expert", expert_status="draft",
            ),
        ]
        active = get_active_experts(skills)
        assert len(active) == 1
        assert active[0].name == "b"

    def test_returns_empty_for_no_experts(self):
        from surogates.tools.builtin.expert import get_active_experts

        skills = [
            SkillDef(name="a", description="d", content="c", source="org"),
        ]
        assert get_active_experts(skills) == []


# =========================================================================
# Skill ORM model expert columns
# =========================================================================


class TestSkillORMExpertColumns:
    """The Skill ORM model has expert-specific columns."""

    def test_skill_model_has_expert_fields(self):
        from surogates.db.models import Skill

        # Check that the column descriptors exist.
        columns = {c.name for c in Skill.__table__.columns}
        assert "type" in columns
        assert "expert_model" in columns
        assert "expert_endpoint" in columns
        assert "expert_adapter" in columns
        assert "expert_config" in columns
        assert "expert_status" in columns
        assert "expert_stats" in columns

    def test_skill_model_defaults(self):
        from surogates.db.models import Skill

        col_defaults = {}
        for col in Skill.__table__.columns:
            if col.server_default is not None:
                col_defaults[col.name] = str(col.server_default.arg)

        assert col_defaults.get("type") == "skill"
        assert col_defaults.get("expert_status") == "draft"
        assert col_defaults.get("expert_config") == "{}"
        assert col_defaults.get("expert_stats") == "{}"


# =========================================================================
# API route: frontmatter manipulation
# =========================================================================


class TestUpdateFrontmatterField:
    """Tests for update_frontmatter_field in loader.py."""

    def test_update_existing_field(self):
        content = (
            "---\nname: test\n"
            "description: desc\n"
            "expert_status: draft\n"
            "---\nBody content\n"
        )
        result = update_frontmatter_field(content, "expert_status", "active")
        assert "expert_status: active" in result
        assert "expert_status: draft" not in result
        assert "Body content" in result

    def test_insert_new_field(self):
        content = (
            "---\nname: test\n"
            "description: desc\n"
            "---\nBody content\n"
        )
        result = update_frontmatter_field(content, "endpoint", "http://e:8000/v1")
        assert "endpoint: http://e:8000/v1" in result
        assert "Body content" in result

    def test_no_frontmatter_returns_unchanged(self):
        content = "No frontmatter here."
        result = update_frontmatter_field(content, "key", "value")
        assert result == content

    def test_frontmatter_at_eof_without_trailing_newline(self):
        content = "---\nname: test\ndescription: desc\n---"
        result = update_frontmatter_field(content, "type", "expert")
        assert "type: expert" in result


class TestExpertStatusConstants:
    """Expert status constants are defined and consistent."""

    def test_constants_exist(self):
        assert EXPERT_STATUS_DRAFT == "draft"
        assert EXPERT_STATUS_COLLECTING == "collecting"
        assert EXPERT_STATUS_ACTIVE == "active"
        assert EXPERT_STATUS_RETIRED == "retired"

    def test_skilldef_default_uses_constant(self):
        s = SkillDef(name="t", description="d", content="c", source="org")
        assert s.expert_status == EXPERT_STATUS_DRAFT

    def test_is_active_expert_uses_constant(self):
        s = SkillDef(
            name="t", description="d", content="c", source="org",
            type="expert", expert_status=EXPERT_STATUS_ACTIVE,
        )
        assert s.is_active_expert is True
