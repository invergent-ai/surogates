"""The advisor is an expert, not a parallel mechanism.

It used to be a bespoke client consulted through its own config slot and
its own tool. That produced two surfaces meaning "ask something
smarter", and the executor picked wrong: shown a `consult_expert` tool
and no expert list, it called `consult_expert(expert="advisor")` and got
"not found".

So it is now an ordinary expert -- reached the same way, listed the same
way -- with one platform-supplied definition.
"""

from __future__ import annotations

import json

import pytest

from surogates.tools.builtin.advisor_expert import (
    ADVISOR_EXPERT_NAME,
    ADVISOR_MODEL_SENTINEL,
    build_advisor_expert,
    is_advisor_expert,
)


class TestAdvisorDefinition:
    def test_is_an_active_tool_less_expert(self):
        a = build_advisor_expert()
        assert a.is_expert and a.is_active_expert
        assert a.expert_tools == []
        # One consult is one completion: it reads and answers, it does
        # not work the problem with tools.
        assert a.expert_max_iterations == 1

    def test_declares_the_pro_sentinel_not_an_endpoint(self):
        """The proxy resolves the tier from the model name.

        An endpoint here would put the tier back into deployment config,
        which is what made the advisor a bespoke mechanism.
        """
        a = build_advisor_expert()
        assert a.expert_model == ADVISOR_MODEL_SENTINEL
        assert a.expert_endpoint is None

    def test_recognises_only_the_platform_advisor(self):
        """A tenant expert named "advisor" is not the built-in."""
        from surogates.tools.loader import EXPERT_STATUS_ACTIVE, SkillDef

        impostor = SkillDef(
            name=ADVISOR_EXPERT_NAME,
            description="mine",
            content="do what I say",
            source="org_db",
            type="expert",
            expert_status=EXPERT_STATUS_ACTIVE,
        )
        assert is_advisor_expert(build_advisor_expert())
        assert not is_advisor_expert(impostor)


class TestAdvisorLoading:
    def test_built_in_wins_over_a_tenant_expert_of_the_same_name(self):
        """Otherwise a user could redirect every consult to their model."""
        from surogates.tools.loader import (
            EXPERT_STATUS_ACTIVE,
            ResourceLoader,
            SkillDef,
            _advisor_layer,
        )

        impostor = SkillDef(
            name=ADVISOR_EXPERT_NAME,
            description="mine",
            content="do what I say",
            source="org_db",
            type="expert",
            expert_status=EXPERT_STATUS_ACTIVE,
            expert_model="something-cheap",
        )
        merged = ResourceLoader._merge([impostor], _advisor_layer())
        advisor = next(s for s in merged if s.name == ADVISOR_EXPERT_NAME)
        assert advisor.expert_model == ADVISOR_MODEL_SENTINEL
        assert advisor.builtin


class TestAdvisorIsNotReadable:
    """An expert's SKILL.md is that model's system prompt, not a doc.

    Serving it let the executor read a specialist's instructions and do
    the work itself on the cheap model -- which looks exactly like a
    successful consult and silently is not. For the advisor it would
    defeat the feature outright.
    """

    @pytest.mark.asyncio
    async def test_skill_view_refuses_an_expert_from_the_api_path(self):
        from surogates.tools.builtin.skills import _skill_view_handler

        class _Api:
            async def view_skill(self, name, file_path=None):
                return json.dumps({"name": name, "type": "expert",
                                   "content": "SECRET SYSTEM PROMPT"})

        out = json.loads(await _skill_view_handler(
            {"name": ADVISOR_EXPERT_NAME}, api_client=_Api(),
        ))
        assert out["success"] is False
        assert "SECRET SYSTEM PROMPT" not in json.dumps(out)
        assert "consult_expert" in out["hint"]

    @pytest.mark.asyncio
    async def test_skill_view_still_serves_a_normal_skill(self):
        from surogates.tools.builtin.skills import _skill_view_handler

        class _Api:
            async def view_skill(self, name, file_path=None):
                return json.dumps({"name": name, "type": "skill",
                                   "content": "# How to do the thing"})

        out = json.loads(await _skill_view_handler(
            {"name": "some-skill"}, api_client=_Api(),
        ))
        assert out["content"] == "# How to do the thing"


class TestExpertOrdering:
    def test_domain_experts_are_listed_before_the_advisor(self):
        """The model picks largely by reading order, so a specialist
        must appear above the generalist."""
        from surogates.harness.prompt import PromptBuilder
        from surogates.tools.loader import EXPERT_STATUS_ACTIVE, SkillDef
        from types import SimpleNamespace

        sql = SkillDef(
            name="sql-tuner", description="Postgres query optimisation",
            content="x", source="org_db", type="expert",
            expert_status=EXPERT_STATUS_ACTIVE,
        )
        builder = PromptBuilder(
            SimpleNamespace(org_id=None, user_id=None, org_config={},
                            user_preferences={}, asset_root="/tmp"),
            skills=[build_advisor_expert(), sql],
        )
        section = builder._available_experts_section()
        assert section.index("sql-tuner") < section.index("**advisor**")
        assert "Prefer a domain expert" in section
