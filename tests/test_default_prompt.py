"""The default prompt: one prompt for every model (measured in benchmarks/prompt_lab)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from surogates.harness.prompt import PromptBuilder
from surogates.harness.prompt_library import PROMPTS_ROOT, PromptLibrary
from surogates.session.models import Session
from surogates.tenant.context import TenantContext
from surogates.tools.loader import ResourceLoader

SKILLS = Path(__file__).parents[1] / "skills"
MODELS = ("surogate/Surogate3.7-35B-A3B", "surogate", "anthropic/claude-sonnet-5", "gpt-5", "deepseek-chat")


def _builder(model: str | None, *, tools: set[str] | None = None) -> PromptBuilder:
    now = datetime.now(timezone.utc)
    session = Session(
        id=uuid4(), user_id=uuid4(), org_id=uuid4(), agent_id="agent-1", channel="web", status="active",
        model=None, config={"workspace_path": "/workspace"}, created_at=now, updated_at=now,
    )
    tenant = TenantContext(
        org_id=session.org_id, user_id=session.user_id, org_config={}, user_preferences={},
        permissions=frozenset(), asset_root="/tmp/test",
    )
    skills = ResourceLoader()._load_skills_from_dir(str(SKILLS), "platform")
    return PromptBuilder(
        tenant, skills=skills, session=session, model_id=model,
        available_tools=tools or {"skill_view", "terminal", "read_file", "write_file", "web_search",
                                  "ask_user_question", "create_artifact", "memory", "todo"},
    )


def _without_vision(prompt: str) -> str:
    return "\n".join(line for line in prompt.split("\n") if not line.startswith("- **Vision**"))


def test_every_model_gets_the_same_prompt():
    prompts = {_without_vision(_builder(m).build()) for m in MODELS}
    assert len(prompts) == 1


@pytest.mark.parametrize("model", MODELS)
def test_the_prompt_is_compact(model):
    prompt = _builder(model).build()
    assert "# How you work" in prompt
    assert "# Execution discipline" not in prompt
    for line in ("**Organisation**", "**Model**", "**Context window**", "**Platform**"):
        assert line not in prompt


def test_the_skills_index_uses_short_lines():
    section = _builder("gpt-5")._skills_section()
    assert "- brainstorming: Agree the design of a large, open-ended build" in section
    assert "You MUST use this before any creative work" not in section
    assert "<available_skills>" in section and "skill_view(name)" in section
    # Skills tied to tools the session lacks stay out (kanban, writing-skills).
    assert "subagent-task-worker" not in section and "writing-skills" not in section


def test_the_prompt_names_no_tool_the_session_lacks():
    # A lean agent (gym tools only) tried terminal because the prompt told it to run `date`.
    lean = _builder("gpt-5", tools={"memory", "todo", "web_search", "web_extract"}).build()
    assert "`date`" not in lean
    assert "use terminal" not in lean and "curl -L" not in lean
    full = _builder("gpt-5", tools={"terminal", "web_search", "web_extract", "read_file"}).build()
    assert "use terminal to download" in full


def test_the_shipped_fragments_validate():
    PromptLibrary(PROMPTS_ROOT).validate()
