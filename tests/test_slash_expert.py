"""Tests for the expert branch in expand_slash_skill."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from surogates.tools.loader import SkillDef


@pytest.fixture
def tenant():
    return SimpleNamespace(org_id=uuid4(), user_id=uuid4(), org_config={})


@pytest.fixture
def session_id():
    return str(uuid4())


def _expert_skill(name: str = "sql_writer") -> SkillDef:
    return SkillDef(
        name=name,
        description="Writes SQL",
        content="You are a SQL expert.",
        source="org",
        type="expert",
        expert_status="active",
        expert_endpoint="http://expert:8000/v1",
        expert_model="qwen2.5-coder-7b",
        expert_tools=["terminal"],
        trigger="SQL queries",
    )


@pytest.mark.asyncio
async def test_returns_kind_expert_and_inlines_deliverable(tenant, session_id):
    from surogates.harness.slash_skill import expand_slash_skill

    expert = _expert_skill()

    async def fake_loader(t, **kwargs):
        return [expert]

    with patch(
        "surogates.harness.slash_skill._load_skills_for_slash",
        new=fake_loader,
    ), patch(
        "surogates.tools.builtin.expert_service.ExpertConsultationService",
    ) as mock_service_cls:
        mock_service = mock_service_cls.return_value
        mock_service.consult = AsyncMock(
            return_value=SimpleNamespace(
                content="SELECT 1;",
                success=True,
                iterations_used=2,
                expert="sql_writer",
                error=None,
            ),
        )

        result = await expand_slash_skill(
            text="/sql_writer write a query for the orders table",
            tools=MagicMock(),
            tenant=tenant,
            session_id=session_id,
            api_client=None,
            session_factory=None,
            session_store=MagicMock(),
            sandbox_pool=MagicMock(),
        )

    assert result is not None
    expanded, name, staged_at, kind = result
    assert name == "sql_writer"
    assert kind == "expert"
    assert staged_at is None
    assert "[Expert sql_writer delivered:]" in expanded
    assert "SELECT 1;" in expanded
    assert "User request: write a query for the orders table" in expanded
    mock_service.consult.assert_awaited_once()


@pytest.mark.asyncio
async def test_regular_skill_path_still_returns_kind_skill(tenant, session_id):
    """Regression: regular /<skill> still goes through skill_view and returns kind='skill'."""
    import json

    from surogates.harness.slash_skill import expand_slash_skill

    regular = SkillDef(
        name="code_review",
        description="Reviews code",
        content="Review.",
        source="org",
        type="skill",
    )

    async def fake_loader(t, **kwargs):
        return [regular]

    tools = MagicMock()
    tools.dispatch = AsyncMock(
        return_value=json.dumps({
            "success": True,
            "content": "Review the code.",
            "staged_at": None,
        }),
    )

    with patch(
        "surogates.harness.slash_skill._load_skills_for_slash",
        new=fake_loader,
    ):
        result = await expand_slash_skill(
            text="/code_review src/foo.py",
            tools=tools,
            tenant=tenant,
            session_id=session_id,
            api_client=None,
            session_factory=None,
            session_store=MagicMock(),
            sandbox_pool=MagicMock(),
        )

    assert result is not None
    expanded, name, staged_at, kind = result
    assert kind == "skill"
    assert name == "code_review"
    assert "Review the code." in expanded


@pytest.mark.asyncio
async def test_inactive_expert_falls_through_to_skill_view(tenant, session_id):
    """A type=expert skill with expert_status != active uses the regular path."""
    import json

    from surogates.harness.slash_skill import expand_slash_skill

    draft = SkillDef(
        name="sql_writer",
        description="Writes SQL",
        content="body",
        source="org",
        type="expert",
        expert_status="draft",
        expert_endpoint="http://expert:8000/v1",
    )

    async def fake_loader(t, **kwargs):
        return [draft]

    tools = MagicMock()
    tools.dispatch = AsyncMock(
        return_value=json.dumps({
            "success": True,
            "content": "body",
            "staged_at": None,
        }),
    )

    with patch(
        "surogates.harness.slash_skill._load_skills_for_slash",
        new=fake_loader,
    ):
        result = await expand_slash_skill(
            text="/sql_writer hello",
            tools=tools,
            tenant=tenant,
            session_id=session_id,
            api_client=None,
            session_factory=None,
            session_store=MagicMock(),
            sandbox_pool=MagicMock(),
        )

    assert result is not None
    _, name, _, kind = result
    assert kind == "skill"
    assert name == "sql_writer"
