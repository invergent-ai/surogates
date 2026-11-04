"""Model prompt profiles: overlay fragments, tool descriptions and skill-index lines for one model family."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from surogates.harness.prompt import PromptBuilder
from surogates.harness.prompt_library import PROMPTS_ROOT, PromptLibrary
from surogates.harness.prompt_profile import (
    PROFILES_ROOT,
    SUROGATE_PROFILE,
    apply_tool_profile,
    resolve_prompt_profile,
    skill_index_entry,
)
from surogates.session.models import Session
from surogates.tenant.context import TenantContext
from surogates.tools.loader import ResourceLoader
from surogates.tools.registry import ToolRegistry
from surogates.tools.runtime import ToolRuntime

OUR_MODEL = "surogate/Surogate3.7-35B-A3B"
SKILLS = Path(__file__).parents[1] / "skills"


def _builder(model: str, *, org_config: dict | None = None, session_config: dict | None = None,
             tools: set[str] | None = None) -> PromptBuilder:
    now = datetime.now(timezone.utc)
    session = Session(
        id=uuid4(), user_id=uuid4(), org_id=uuid4(), agent_id="agent-1", channel="web", status="active",
        model=model, config={"workspace_path": "/workspace", **(session_config or {})},
        created_at=now, updated_at=now,
    )
    tenant = TenantContext(
        org_id=session.org_id, user_id=session.user_id, org_config=org_config or {}, user_preferences={},
        permissions=frozenset(), asset_root="/tmp/test",
    )
    skills = ResourceLoader()._load_skills_from_dir(str(SKILLS), "platform")
    return PromptBuilder(
        tenant, skills=skills, session=session,
        available_tools=tools or {"skill_view", "terminal", "read_file", "write_file", "web_search",
                                  "ask_user_question", "create_artifact", "memory", "todo"},
    )


# ------------------------------------------------------------------ resolution
@pytest.mark.parametrize("model,expected", [
    (OUR_MODEL, SUROGATE_PROFILE),
    ("surogate3.7", SUROGATE_PROFILE),
    ("Surogate3.7-35B-A3B", SUROGATE_PROFILE),
    # Tier sentinels: what serves them is the gateway's choice (Pro is Claude).
    ("surogate", None),
    ("surogate-pro", None),
    ("anthropic/claude-sonnet-5", None),
    ("gpt-5", None),
])
def test_the_model_id_selects_the_profile(model, expected, monkeypatch):
    monkeypatch.delenv("SUROGATES_PROMPT_PROFILES", raising=False)
    assert resolve_prompt_profile(model) == expected


def test_a_deployment_maps_the_base_tier_without_touching_pro(monkeypatch):
    monkeypatch.setenv("SUROGATES_PROMPT_PROFILES", "surogate=surogate")
    assert resolve_prompt_profile("surogate") == SUROGATE_PROFILE
    assert resolve_prompt_profile("surogate-pro") is None
    assert resolve_prompt_profile("anthropic/claude-sonnet-5") is None
    monkeypatch.setenv("SUROGATES_PROMPT_PROFILES", "surogate/surogate3.7-35b-a3b=default")
    assert resolve_prompt_profile(OUR_MODEL) is None


def test_explicit_settings_win_in_order(monkeypatch):
    monkeypatch.setenv("SUROGATES_PROMPT_PROFILES", "surogate=surogate")
    assert resolve_prompt_profile(OUR_MODEL, org_config={"prompt_profile": "default"}) is None
    assert resolve_prompt_profile("gpt-5", session_config={"prompt_profile": "surogate"},
                                  org_config={"prompt_profile": "default"}) == SUROGATE_PROFILE
    # The per-model form: only the named model, or "*" for the rest.
    per_model = {"prompt_profile": {"surogate": "surogate", "*": "default"}}
    assert resolve_prompt_profile("surogate", org_config=per_model) == SUROGATE_PROFILE
    assert resolve_prompt_profile("surogate-pro", org_config=per_model) is None
    assert resolve_prompt_profile("surogate-pro", org_config={"prompt_profile": {"surogate": "surogate"}}) is None


def test_an_unknown_profile_falls_back_to_the_default_prompt(monkeypatch):
    monkeypatch.setenv("SUROGATES_PROMPT_PROFILES", "surogate/surogate3.7-35b-a3b=no-such-profile")
    assert resolve_prompt_profile(OUR_MODEL) is None


# ------------------------------------------------------------------ library overlays
def test_overlays_shadow_base_fragments_and_share_the_cache():
    base = PromptLibrary()
    view = base.for_profile(SUROGATE_PROFILE)
    assert view.get("guidance/working_principles") != base.get("guidance/working_principles")
    assert view.get("guidance/memory") != base.get("guidance/memory")
    # A key the profile does not overlay reads the base fragment.
    assert view.get("guidance/coordinator") == base.get("guidance/coordinator")
    assert view._body_cache is base._body_cache
    assert base.for_profile(None) is base


def test_every_overlay_shadows_a_real_fragment():
    # A typo in an overlay's path would silently never load.
    new_keys = {"guidance/skills_index", "guidance/kb_actions"}
    for path in (PROFILES_ROOT / SUROGATE_PROFILE).rglob("*.md"):
        key = str(path.relative_to(PROFILES_ROOT / SUROGATE_PROFILE).with_suffix(""))
        assert key in new_keys or (PROMPTS_ROOT / f"{key}.md").is_file(), key


def test_validate_parses_profile_overlays():
    PromptLibrary().validate()


# ------------------------------------------------------------------ builder
def test_the_profile_prompt_is_compact_and_has_no_execution_discipline_block(monkeypatch):
    monkeypatch.delenv("SUROGATES_PROMPT_PROFILES", raising=False)
    default = _builder("surogate").build()
    ours = _builder(OUR_MODEL).build()
    assert "# Execution discipline" in default and "# Execution discipline" not in ours
    assert "# How you work" in ours
    assert len(ours) < 0.5 * len(default)
    # Compact context: no org id or platform architecture line, vision still stated.
    assert "**Organisation**" in default and "**Organisation**" not in ours
    assert "**Vision**" in ours


def test_without_a_profile_the_prompt_is_unchanged(monkeypatch):
    monkeypatch.delenv("SUROGATES_PROMPT_PROFILES", raising=False)
    builder = _builder("surogate")
    assert builder.prompt_profile is None
    prompt = builder.build()
    assert "## Skills (mandatory)" in prompt
    assert "If you think there is even a 1% chance a skill might apply" in prompt


def test_the_skills_index_uses_the_profile_lines(monkeypatch):
    monkeypatch.delenv("SUROGATES_PROMPT_PROFILES", raising=False)
    section = _builder(OUR_MODEL)._skills_section()
    assert "- brainstorming: Agree the design of a large, open-ended build" in section
    assert "You MUST use this before any creative work" not in section
    assert "<available_skills>" in section and "skill_view(name)" in section
    # Skills tied to tools the session lacks stay out (kanban, writing-skills).
    assert "subagent-task-worker" not in section and "writing-skills" not in section


def test_skill_index_entry():
    assert skill_index_entry(None, "docx", "long") == "long"
    assert skill_index_entry(SUROGATE_PROFILE, "no-such-skill", "kept") == "kept"
    assert skill_index_entry(SUROGATE_PROFILE, "docx", "long").startswith("Create, edit or convert Word")


# ------------------------------------------------------------------ tool descriptions
def _registry_schemas() -> list[dict]:
    reg = ToolRegistry()
    ToolRuntime(reg).register_builtins()
    return reg.get_schemas()


def test_tool_profile_changes_descriptions_only():
    schemas = _registry_schemas()
    before = copy.deepcopy(schemas)
    out = apply_tool_profile(schemas, SUROGATE_PROFILE)
    assert schemas == before  # input not mutated
    by_name = {s["function"]["name"]: s for s in out}
    orig = {s["function"]["name"]: s for s in before}
    changed = 0
    for name, s in by_name.items():
        p, q = s["function"]["parameters"], orig[name]["function"]["parameters"]
        assert p.get("required") == q.get("required"), name
        assert set(p.get("properties", {})) == set(q.get("properties", {})), name
        for prop, spec in p.get("properties", {}).items():
            a = {k: v for k, v in spec.items() if k != "description"}
            b = {k: v for k, v in q["properties"][prop].items() if k != "description"}
            assert a == b, (name, prop)
        changed += s["function"]["description"] != orig[name]["function"]["description"]
    assert changed >= 20
    assert apply_tool_profile(schemas, None) is schemas


def test_tools_yaml_names_real_tools_and_parameters():
    data = yaml.safe_load((PROFILES_ROOT / SUROGATE_PROFILE / "tools.yaml").read_text())
    by_name = {s["function"]["name"]: s["function"] for s in _registry_schemas()}
    for tool, override in data.items():
        assert tool in by_name, tool
        props = by_name[tool]["parameters"].get("properties", {})
        for param in (override.get("parameters") or {}):
            assert param in props, (tool, param)


def test_skills_yaml_names_real_skills():
    data = yaml.safe_load((PROFILES_ROOT / SUROGATE_PROFILE / "skills.yaml").read_text())
    names = {s.name for s in ResourceLoader()._load_skills_from_dir(str(SKILLS), "platform")}
    assert set(data) <= names


def test_the_profile_leaves_the_office_tools_and_skill_alone():
    # office_read / office_edit and the office skill are tuned on their own.
    tools = yaml.safe_load((PROFILES_ROOT / SUROGATE_PROFILE / "tools.yaml").read_text())
    skills = yaml.safe_load((PROFILES_ROOT / SUROGATE_PROFILE / "skills.yaml").read_text())
    assert not [t for t in tools if t.startswith("office")]
    assert "office" not in skills


def test_the_workers_model_id_selects_the_profile_on_a_first_wake(monkeypatch):
    # A new session's row has no model yet: the worker records it while building the prompt.
    monkeypatch.delenv("SUROGATES_PROMPT_PROFILES", raising=False)
    now = datetime.now(timezone.utc)
    session = Session(id=uuid4(), user_id=uuid4(), org_id=uuid4(), agent_id="a", channel="api", status="active",
                      model=None, config={}, created_at=now, updated_at=now)
    tenant = TenantContext(org_id=session.org_id, user_id=session.user_id, org_config={}, user_preferences={},
                           permissions=frozenset(), asset_root="/tmp/test")
    assert PromptBuilder(tenant, session=session).prompt_profile is None
    assert PromptBuilder(tenant, session=session, model_id=OUR_MODEL).prompt_profile == SUROGATE_PROFILE
