"""With the brainstorming gate off, nothing in the prompt steers the agent into brainstorming.

On PROD, 41 sessions of agents with the gate off loaded the skill unprompted: the gate removed
only its own guidance fragment, while the skill's "You MUST use this before any creative work"
description stayed in the index and the skills guidance still said "brainstorming first".
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from surogates.harness.prompt import PromptBuilder
from surogates.session.models import Session
from surogates.tenant.context import TenantContext
from surogates.tools.loader import SkillDef


def _builder(gate: bool) -> PromptBuilder:
    now = datetime.now(timezone.utc)
    session = Session(
        id=uuid4(), user_id=uuid4(), org_id=uuid4(), agent_id="agent-1", channel="web", status="active",
        config={}, created_at=now, updated_at=now,
    )
    tenant = TenantContext(
        org_id=session.org_id, user_id=session.user_id, org_config={}, user_preferences={},
        permissions=frozenset(), asset_root="/tmp/test",
    )
    skills = [
        SkillDef(name="brainstorming", description="You MUST use this before any creative work", content="b", source="platform"),
        SkillDef(name="pdf", description="Work with PDF files", content="p", source="platform"),
    ]
    return PromptBuilder(
        tenant, skills=skills, session=session, available_tools={"skill_view"}, brainstorming_gate=gate,
    )


def test_gate_off_leaves_brainstorming_out_of_the_prompt():
    builder = _builder(gate=False)
    assert "brainstorming" not in builder._skills_section()
    assert "pdf" in builder._skills_section()
    assert "brainstorming" not in builder._tool_guidance_section()


def test_gate_on_indexes_the_skill_and_requires_it():
    builder = _builder(gate=True)
    assert "brainstorming" in builder._skills_section()
    assert 'skill_view("brainstorming")' in builder._tool_guidance_section()
