"""Tests for the Experts feature -- fine-tuned SLMs as skills.

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


class TestPromptBuilderExpertGuidance:
    """PromptBuilder injects expert guidance and lists experts."""

    @pytest.fixture
    def tenant(self):
        return SimpleNamespace(
            org_id=uuid4(),
            user_id=uuid4(),
            org_config={"default_model": "gpt-4o"},
            user_preferences={},
            asset_root="/tmp/test_assets",
        )

    def test_expert_guidance_injected_when_tool_available(self, tenant):
        from surogates.harness.prompt import EXPERT_GUIDANCE, PromptBuilder

        pb = PromptBuilder(
            tenant=tenant,
            available_tools={"consult_expert", "memory"},
        )
        section = pb._tool_guidance_section()
        assert EXPERT_GUIDANCE in section

    def test_expert_guidance_not_injected_without_tool(self, tenant):
        from surogates.harness.prompt import EXPERT_GUIDANCE, PromptBuilder

        pb = PromptBuilder(
            tenant=tenant,
            available_tools={"memory"},
        )
        section = pb._tool_guidance_section()
        assert EXPERT_GUIDANCE not in section

    def test_skills_section_separates_experts_from_skills(self, tenant):
        from surogates.harness.prompt import PromptBuilder

        skills = [
            {"name": "code-review", "description": "Reviews code", "type": "skill", "trigger": "/review"},
            {
                "name": "sql_writer",
                "description": "Writes SQL",
                "type": "expert",
                "expert_tools": ["terminal"],
                "expert_stats": {"total_uses": 100, "total_successes": 94},
            },
        ]

        pb = PromptBuilder(
            tenant=tenant,
            skills=skills,
            available_tools={"consult_expert"},
        )
        section = pb._skills_section()

        assert "# Available Skills" in section
        assert "# Available Experts" in section
        assert "code-review" in section
        assert "sql_writer" in section
        assert "consult_expert" in section
        assert "94%" in section
        assert "terminal" in section

    def test_skills_section_handles_skilldefs(self, tenant):
        from surogates.harness.prompt import PromptBuilder

        skills = [
            SkillDef(
                name="my-expert",
                description="An expert",
                content="body",
                source="org",
                type="expert",
                expert_tools=["terminal"],
                expert_status="active",
            ),
        ]

        pb = PromptBuilder(tenant=tenant, skills=skills)
        section = pb._skills_section()

        assert "# Available Experts" in section
        assert "my-expert" in section


# =========================================================================
# Tool router location
# =========================================================================


class TestToolRouterExpertLocation:
    """consult_expert is routed to HARNESS location."""

    def test_consult_expert_in_tool_locations(self):
        from surogates.tools.router import TOOL_LOCATIONS, ToolLocation

        assert "consult_expert" in TOOL_LOCATIONS
        assert TOOL_LOCATIONS["consult_expert"] == ToolLocation.HARNESS


# =========================================================================
# ToolRuntime registers expert
# =========================================================================


class TestToolRuntimeRegistersExpert:
    """ToolRuntime.register_builtins() includes the expert module."""

    def test_expert_registered(self):
        from surogates.tools.registry import ToolRegistry
        from surogates.tools.runtime import ToolRuntime

        reg = ToolRegistry()
        runtime = ToolRuntime(reg)
        runtime.register_builtins()
        assert reg.has("consult_expert")


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
