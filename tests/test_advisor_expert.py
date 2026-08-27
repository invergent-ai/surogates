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


class TestAdvisorNeedsNoEndpoint:
    """The whole point: a platform expert declares a model, not a URL.

    ``ExpertConsultationService`` rejects an expert with no endpoint,
    because a tenant expert dials an arbitrary upstream and must say
    where. The advisor rides the session's client instead -- its tier
    comes from the model sentinel, which the proxy resolves. Without
    this the consult fails with "Expert 'advisor' has no endpoint
    configured", which is exactly what shipped first.
    """

    @pytest.mark.asyncio
    async def test_consult_succeeds_with_a_supplied_client(self):
        from unittest.mock import AsyncMock
        from types import SimpleNamespace
        from surogates.tools.builtin.expert_service import (
            ExpertConsultationService,
        )

        advisor = build_advisor_expert()
        assert advisor.expert_endpoint is None  # the precondition

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=AsyncMock(return_value=SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(
                        content="Ship the smaller fix first.",
                        tool_calls=None,
                    ),
                    finish_reason="stop",
                )],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            )),
        )))

        service = ExpertConsultationService(
            tenant=SimpleNamespace(org_id=None, user_id=None),
            session_id=__import__("uuid").uuid4(),
            tool_registry=SimpleNamespace(get_schemas=lambda names=None: []),
            session_store=AsyncMock(),
        )
        result = await service.consult(
            expert=advisor, task="what next?", client=client,
        )

        assert result.success, result.error
        assert "Ship the smaller fix first." in result.content
        # It asked for the Pro tier by name; the proxy does the routing.
        sent = client.chat.completions.create.await_args.kwargs
        assert sent["model"] == ADVISOR_MODEL_SENTINEL

    @pytest.mark.asyncio
    async def test_tenant_expert_without_an_endpoint_still_fails(self):
        """The requirement stays for experts that dial their own upstream."""
        from unittest.mock import AsyncMock
        from types import SimpleNamespace
        from surogates.tools.loader import EXPERT_STATUS_ACTIVE, SkillDef
        from surogates.tools.builtin.expert_service import (
            ExpertConsultationService,
        )

        rogue = SkillDef(
            name="sql", description="d", content="c", source="org_db",
            type="expert", expert_status=EXPERT_STATUS_ACTIVE,
        )
        service = ExpertConsultationService(
            tenant=SimpleNamespace(org_id=None, user_id=None),
            session_id=__import__("uuid").uuid4(),
            tool_registry=SimpleNamespace(get_schemas=lambda names=None: []),
            session_store=AsyncMock(),
        )
        result = await service.consult(expert=rogue, task="x")
        assert not result.success
        assert "no endpoint configured" in result.error


class TestConsultFooter:
    """A consult reminder in last position, not only mid-prompt.

    The mid-prompt guidance fragment renders (6KB, imperative wording)
    and the executor still answered a three-way architecture question in
    one iteration without consulting anything. The harness already
    places late-landing directives at the end for the same reason -- see
    the artifact-in-channel hint -- so the reminder goes there too.
    """

    @staticmethod
    def _builder(skills, tools):
        from types import SimpleNamespace
        from surogates.harness.prompt import PromptBuilder

        return PromptBuilder(
            SimpleNamespace(org_id=None, user_id=None, org_config={},
                            user_preferences={}, asset_root="/tmp"),
            skills=skills, available_tools=tools,
        )

    def test_footer_is_the_last_thing_in_the_prompt(self):
        b = self._builder([build_advisor_expert()], {"consult_expert"})
        prompt = b.build()
        assert "## Before you commit" in prompt
        tail = prompt[prompt.index("## Before you commit"):]
        # Nothing may follow it -- that is the whole point of the placement.
        assert "\n# " not in tail

    def test_no_footer_without_the_tool(self):
        b = self._builder([build_advisor_expert()], set())
        assert b._expert_footer() == ""

    def test_no_footer_without_any_active_expert(self):
        from surogates.tools.loader import SkillDef

        plain = SkillDef(name="s", description="d", content="c",
                         source="org_db")
        b = self._builder([plain], {"consult_expert"})
        assert b._expert_footer() == ""
