"""A skill's ``requires_agents``: shown only while those sub-agents are in the catalog."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from surogates.harness.prompt import PromptBuilder
from surogates.session.models import Session
from surogates.tenant.context import TenantContext
from surogates.tools.loader import AgentDef, SkillDef, _parse_skill_frontmatter

ARBOR_SKILL_MD = """---
name: arbor-ideate
description: Hard-gated ideation for an Arbor research run.
requires_agents:
  - arbor-executor
---
Write the first-principles probe before any hypothesis.
"""


def _skill(**fields) -> SkillDef:
    return SkillDef(content="body", source="platform", **fields)


def _builder(agents: list[AgentDef], preloaded: list[str] | None = None) -> PromptBuilder:
    now = datetime.now(timezone.utc)
    session = Session(
        id=uuid4(), user_id=uuid4(), org_id=uuid4(), agent_id="agent-1", channel="web", status="active",
        config={"preloaded_skills": preloaded or []}, created_at=now, updated_at=now,
    )
    tenant = TenantContext(
        org_id=session.org_id, user_id=session.user_id, org_config={}, user_preferences={},
        permissions=frozenset(), asset_root="/tmp/test",
    )
    skills = [
        _skill(name="arbor-ideate", description="Ideation for a research run", requires_agents=["arbor-executor"]),
        _skill(name="pdf", description="Work with PDF files"),
    ]
    return PromptBuilder(tenant, skills=skills, session=session, available_agents=agents)


def _executor(enabled: bool = True) -> AgentDef:
    return AgentDef(name="arbor-executor", description="Runs one experiment", system_prompt="", source="platform", enabled=enabled)


def test_frontmatter_requires_agents_is_parsed():
    assert _parse_skill_frontmatter(ARBOR_SKILL_MD, "x")["requires_agents"] == ["arbor-executor"]


def test_skill_is_indexed_only_while_its_sub_agent_is_available():
    assert "arbor-ideate" in _builder([_executor()])._skills_section()

    without = _builder([])._skills_section()
    assert "arbor-ideate" not in without
    assert "pdf" in without

    assert "arbor-ideate" not in _builder([_executor(enabled=False)])._skills_section()


def test_a_preloaded_skill_is_inlined_whatever_its_gate():
    # /auto-research preloads arbor-coordinator; the gate only governs the index.
    assert "## arbor-ideate" in _builder([], preloaded=["arbor-ideate"])._preloaded_skills_section()
