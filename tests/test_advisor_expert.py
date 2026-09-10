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
)


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
