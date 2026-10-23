"""Prompt texts name only tools the agent has."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from surogates.harness.loop_messages import _page_view_context_note
from surogates.harness.prompt import PromptBuilder
from surogates.session.models import Session
from surogates.tenant.context import TenantContext
from surogates.tools.loader import SkillDef


def _builder(tools: set[str]) -> PromptBuilder:
    now = datetime.now(timezone.utc)
    session = Session(
        id=uuid4(), user_id=uuid4(), org_id=uuid4(), agent_id="agent-1", channel="web", status="active",
        config={}, created_at=now, updated_at=now,
    )
    tenant = TenantContext(
        org_id=session.org_id, user_id=session.user_id, org_config={}, user_preferences={},
        permissions=frozenset(), asset_root="/tmp/test",
    )
    skills = [SkillDef(name="pdf", description="Work with PDF files", content="p", source="platform")]
    return PromptBuilder(tenant, skills=skills, session=session, available_tools=tools)


def test_page_note_points_at_a_section_not_the_whole_page():
    note = _page_view_context_note({"page": "Vault", "docPath": "work/settings/vault"})
    assert "work/settings/vault" in note
    assert "section" in note
    assert "read_doc(" not in note


def test_skills_index_never_names_skill_view_without_it():
    text = _builder({"read_file"})._skills_section()
    assert "pdf" in text
    assert "skill_view" not in text
    assert "Only proceed without loading" not in text
    assert "context only" in text


def test_skills_index_unchanged_with_skill_view():
    text = _builder({"skill_view"})._skills_section()
    assert "`skill_view(name)`" in text
    assert "Only proceed without loading a skill" in text
