"""An expert is not listed, and its view gives no hint to consult it, where the session has no ``consult_expert``.

HO-X reads with the advisor off (consult_expert in the template's excluded_tools, surogates 15af7f4e0, 11-20):
``skills_list`` still listed the expert "advisor", and ``skill_view("advisor")`` answered with the hint
``Consult it instead: consult_expert(expert="advisor", ...)``.  The model then called the tool it does not have,
refused "Unknown tool": in 22 of 264 attempts of one arm (8 %), 15 of 240 of another (6 %).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from surogates.harness.prompt import PromptBuilder
from surogates.tools.builtin.advisor_expert import build_advisor_expert
from surogates.tools.builtin.skills import _skill_view_handler, _skills_list_handler
from surogates.tools.skill_visibility import SkillScope, expert_unreachable, skill_scope
from tests.test_case_tool_gate import _registry
from tests.test_fallback_skills_hidden import TENANT, catalog, the_worker_reads_the_repositorys_skills  # noqa: F401

ADVISOR_OFF = {"excluded_tools": ["consult_expert"]}


def _scoped(config: dict):
    """A session with *config* whose skills include the built-in advisor, scoped for the turn's tool calls."""
    from tests.test_case_tool_gate import _chat
    from tests.test_prompt_honours_excluded_tools import _harness as harness_for

    session = _chat(config)
    registry = _registry()
    builder = PromptBuilder(
        TENANT, skills=[*catalog(), build_advisor_expert()], session=session, available_tools=set(registry.tool_names),
    )
    harness = harness_for(session, builder)
    harness._tools = registry
    harness._scope_skills(session)
    return harness, session


def _api(listed: list[dict]):
    """The api as the worker reaches it: it lists every skill, experts too (the Studio renders them)."""
    return SimpleNamespace(
        list_skills=None,
        _skills=listed,
        view_skill=AsyncMock(return_value=json.dumps({"name": "advisor", "type": "expert", "content": "secret"})),
    )


async def _listed_through_the_api(config: dict) -> list[str]:
    from surogates.harness.api_client import HarnessAPIClient

    client = HarnessAPIClient.__new__(HarnessAPIClient)
    client._session_id = None
    client._get = AsyncMock(return_value={"skills": [
        {"name": "advisor", "type": "expert", "category": None, "description": "a stronger model"},
        {"name": "schedule", "type": "skill", "category": "productivity", "description": "schedules"},
    ]})
    answer = json.loads(await _skills_list_handler({}, api_client=client, session_config=config))
    return [entry["name"] for entry in answer["skills"]]


async def test_without_consult_expert_skills_list_does_not_list_the_advisor():
    _scoped(ADVISOR_OFF)
    assert "advisor" in skill_scope.get().hidden
    assert await _listed_through_the_api(ADVISOR_OFF) == ["schedule"]


async def test_without_consult_expert_viewing_the_advisor_names_no_tool():
    _scoped(ADVISOR_OFF)
    api = _api([])
    for kwargs in ({"api_client": api, "session_config": ADVISOR_OFF}, {"tenant": TENANT, "session_config": ADVISOR_OFF}):
        raw = await _skill_view_handler({"name": "advisor"}, **kwargs)
        answer = json.loads(raw)
        assert answer["success"] is False
        assert "consult_expert" not in raw and "Consult" not in raw
    api.view_skill.assert_not_awaited()


async def test_with_consult_expert_nothing_changes():
    """Where the advisor can be consulted, its view still says how, and the api's list is as before."""
    harness, session = _scoped({})
    assert "advisor" not in skill_scope.get().hidden
    assert await _listed_through_the_api({}) == ["advisor", "schedule"]
    raw = await _skill_view_handler({"name": "advisor"}, api_client=_api([]), session_config={})
    assert 'consult_expert(expert=\\"advisor\\"' in raw
    assert "# Available Experts" in await harness._build_system_prompt(session)


async def test_the_prompt_and_the_skill_tools_agree_on_the_advisor():
    harness, session = _scoped(ADVISOR_OFF)
    prompt = await harness._build_system_prompt(session)
    assert "# Available Experts" not in prompt and "advisor" in skill_scope.get().hidden


def test_only_an_expert_is_unreachable_without_consult_expert():
    assert expert_unreachable({"type": "expert"}, {"consult_expert"})
    assert not expert_unreachable({"type": "expert"}, {"terminal"})
    assert not expert_unreachable({"type": "skill"}, {"consult_expert"})
    assert expert_unreachable(build_advisor_expert(), frozenset({"consult_expert"}))


async def test_an_expert_the_prompt_never_had_is_not_listed_or_hinted_either():
    """The api lists from its own catalog: an expert made after the wake, or the advisor a Pro-tier session's
    roster leaves out, is no name in the turn's scope; it goes by its type."""
    from surogates.harness.api_client import HarnessAPIClient

    token = skill_scope.set(SkillScope(hidden=frozenset(), experts_reachable=False))
    try:
        client = HarnessAPIClient.__new__(HarnessAPIClient)
        client._session_id = None
        client._get = AsyncMock(return_value={"skills": [
            {"name": "sql-writer", "type": "expert", "category": "data"},
            {"name": "schedule", "type": "skill", "category": "productivity"},
        ]})
        answer = json.loads(await _skills_list_handler({}, api_client=client, session_config=ADVISOR_OFF))
        assert [entry["name"] for entry in answer["skills"]] == ["schedule"]
        assert answer["count"] == 1 and answer["categories"] == ["productivity"]
        api = SimpleNamespace(view_skill=AsyncMock(return_value=json.dumps({"name": "sql-writer", "type": "expert"})))
        raw = await _skill_view_handler({"name": "sql-writer"}, api_client=api, session_config=ADVISOR_OFF)
        assert json.loads(raw)["success"] is False and "consult_expert" not in raw
    finally:
        skill_scope.reset(token)

