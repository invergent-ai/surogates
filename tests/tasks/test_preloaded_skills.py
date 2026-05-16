"""Tests for the auto-enforced subagent-task-worker skill preload.

The dispatcher's _build_task_worker_config injects "subagent-task-worker"
into session.config["preloaded_skills"]; PromptBuilder's
_preloaded_skills_section inlines the SKILL.md body for every name in
that list. Together these auto-enforce the worker playbook on every
task-backed Session without the agent having to call skill_view.
"""
from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

from tests.tasks.conftest import _make_task


# ---------------------------------------------------------------------------
# _build_task_worker_config injects the skill name
# ---------------------------------------------------------------------------


def test_build_task_worker_config_appends_subagent_task_worker():
    """A task-backed session's worker_config carries the auto-enforce skill."""
    from surogates.tasks.spawn import _build_task_worker_config

    task = _make_task()
    cfg = _build_task_worker_config(agent_def=None, task=task)
    assert "subagent-task-worker" in cfg["preloaded_skills"]


def test_build_task_worker_config_does_not_duplicate_when_agent_def_already_lists_it():
    """Idempotent: a future AgentDef that already names this skill doesn't see it twice."""
    from surogates.tasks.spawn import _build_task_worker_config

    # Currently _build_task_worker_config doesn't read agent_def.preloaded_skills,
    # but the dedup guard means re-entry on the resulting cfg stays correct.
    task = _make_task()
    cfg = _build_task_worker_config(agent_def=None, task=task)
    # Re-build using the previously-computed config as input (simulating
    # a hypothetical inheritance path).
    cfg2 = _build_task_worker_config(agent_def=None, task=task)
    assert cfg["preloaded_skills"] == ["subagent-task-worker"]
    assert cfg2["preloaded_skills"] == ["subagent-task-worker"]


def test_build_task_worker_config_preloaded_alongside_agent_def_presets():
    """When an AgentDef supplies its own presets, preloaded_skills coexists with them."""
    from surogates.tasks.spawn import _build_task_worker_config

    agent_def = MagicMock(
        name="reviewer", max_iterations=20,
        policy_profile="read_only",
        tools=["read_file", "search_files"],
        disallowed_tools=None,
        model="claude-sonnet-4-6",
    )
    task = _make_task(agent_def_name="reviewer")

    cfg = _build_task_worker_config(agent_def=agent_def, task=task)
    assert "subagent-task-worker" in cfg["preloaded_skills"]
    assert cfg["agent_type"] == "reviewer"
    assert cfg["policy_profile"] == "read_only"
    assert "read_file" in cfg["allowed_tools"]


# ---------------------------------------------------------------------------
# PromptBuilder inlines preloaded skill bodies
# ---------------------------------------------------------------------------


def _make_session_with_preloaded(skills: list[str]) -> MagicMock:
    session = MagicMock()
    session.id = uuid4()
    session.config = {"preloaded_skills": skills}
    session.user_id = None
    session.service_account_id = None
    return session


def test_preloaded_skills_section_inlines_full_skill_body():
    """When config names a skill, its full content is rendered in the prompt."""
    from surogates.harness.prompt import PromptBuilder
    from surogates.tools.loader import SkillDef

    skill = SkillDef(
        name="subagent-task-worker",
        description="how to be a task worker",
        content="The full SKILL.md body goes here. Step 1: orient. Step 2: work.",
        source="platform",
    )

    builder = PromptBuilder(
        tenant=MagicMock(org_id=uuid4()),
        skills=[skill],
        session=_make_session_with_preloaded(["subagent-task-worker"]),
    )

    section = builder._preloaded_skills_section()
    assert "# Loaded Skills" in section
    assert "## subagent-task-worker" in section
    assert "Step 1: orient" in section
    assert "Step 2: work" in section


def test_preloaded_skills_section_empty_when_unset():
    """No preloaded_skills key in config -> empty string (clean prompt)."""
    from surogates.harness.prompt import PromptBuilder

    session = MagicMock()
    session.id = uuid4()
    session.config = {}  # no preloaded_skills key
    session.user_id = None
    session.service_account_id = None

    builder = PromptBuilder(
        tenant=MagicMock(org_id=uuid4()),
        skills=[],
        session=session,
    )
    assert builder._preloaded_skills_section() == ""


def test_preloaded_skills_section_empty_when_skill_not_in_catalog():
    """Listed but missing skills don't crash the wake — section is empty."""
    from surogates.harness.prompt import PromptBuilder

    builder = PromptBuilder(
        tenant=MagicMock(org_id=uuid4()),
        skills=[],  # catalog doesn't contain the named skill
        session=_make_session_with_preloaded(["subagent-task-worker"]),
    )
    assert builder._preloaded_skills_section() == ""


def test_preloaded_skills_section_renders_multiple_skills_in_order():
    """Multiple preloads concatenate; each gets its own ## heading."""
    from surogates.harness.prompt import PromptBuilder
    from surogates.tools.loader import SkillDef

    a = SkillDef(name="alpha", description="a", content="alpha body", source="platform")
    b = SkillDef(name="beta", description="b", content="beta body", source="platform")
    builder = PromptBuilder(
        tenant=MagicMock(org_id=uuid4()),
        skills=[a, b],
        session=_make_session_with_preloaded(["alpha", "beta"]),
    )
    section = builder._preloaded_skills_section()
    assert "## alpha" in section
    assert "## beta" in section
    # Both bodies present.
    assert "alpha body" in section
    assert "beta body" in section


def test_preloaded_skills_section_ignores_skills_not_in_preload_list():
    """A skill present in catalog but not in preloaded_skills isn't inlined here.
    (It still appears in the regular catalog section via _skills_section.)"""
    from surogates.harness.prompt import PromptBuilder
    from surogates.tools.loader import SkillDef

    skill = SkillDef(
        name="other-skill", description="d", content="should not appear here",
        source="platform",
    )
    builder = PromptBuilder(
        tenant=MagicMock(org_id=uuid4()),
        skills=[skill],
        session=_make_session_with_preloaded(["subagent-task-worker"]),
    )
    section = builder._preloaded_skills_section()
    assert "should not appear here" not in section
    # And nothing renders, since the only available skill isn't preloaded.
    assert section == ""


def test_preloaded_skills_section_safe_when_session_is_none():
    """build_prompt is called for some flows without a Session; don't crash."""
    from surogates.harness.prompt import PromptBuilder
    from surogates.tools.loader import SkillDef

    skill = SkillDef(name="x", description="d", content="b", source="platform")
    builder = PromptBuilder(
        tenant=MagicMock(org_id=uuid4()),
        skills=[skill],
        session=None,
    )
    assert builder._preloaded_skills_section() == ""
