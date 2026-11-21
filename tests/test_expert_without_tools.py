"""An expert sent no tools is told so, and a consult that returns no text is not a success."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.session.events import EventType
from surogates.tools.builtin.expert_loop import _build_expert_system_prompt
from surogates.tools.builtin.expert_service import ExpertConsultationService
from surogates.tools.loader import SkillDef

WITH_TOOLS = (
    "You are running as a sub-agent inside a larger system. Complete the given task using your "
    "available tools and return your final answer as text. Be concise and precise."
)


def _expert(tools=None) -> SkillDef:
    return SkillDef(
        name="reviewer", description="Reviews work", content="Expert body.", source="org", type="expert",
        expert_status="active", expert_model="m", expert_endpoint="http://e:8000/v1", expert_tools=tools,
        expert_max_iterations=1,
    )


@pytest.mark.parametrize("declared, sent", [(None, None), ([], None), (["terminal"], [])])
def test_an_expert_sent_no_tools_is_told_it_has_none(declared, sent):
    prompt = _build_expert_system_prompt(_expert(declared), sent)
    assert "You have no tools here" in prompt
    assert "available tools" not in prompt
    assert "Expert body." in prompt


def test_an_expert_with_tools_reads_what_it_read_before():
    prompt = _build_expert_system_prompt(_expert(["terminal"]), ["terminal"])
    assert WITH_TOOLS in prompt
    assert "You have access to these tools: terminal." in prompt
    assert "You have no tools" not in prompt


def _service(store) -> ExpertConsultationService:
    return ExpertConsultationService(
        tenant=SimpleNamespace(org_id=uuid4(), user_id=uuid4(), org_config={}),
        session_id=uuid4(), tool_registry=MagicMock(), session_store=store,
    )


def _client(content, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=message)]))
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


@pytest.mark.parametrize("content", ["", "   \n", None])
async def test_a_consult_with_no_text_is_a_failure_the_caller_reads(content, monkeypatch):
    monkeypatch.setattr("surogates.config.load_settings", lambda: SimpleNamespace(platform_api_url="", llm=SimpleNamespace()))
    store = AsyncMock()
    result = await _service(store).consult(expert=_expert(), task="review", client=_client(content))
    assert result.success is False
    assert "returned no text" in result.content and "own judgment" in result.content
    assert result.iterations_used == 1
    assert [c.args[1] for c in store.emit_event.await_args_list] == [
        EventType.EXPERT_DELEGATION, EventType.EXPERT_FAILURE,
    ]


async def test_a_consult_with_text_stays_a_success(monkeypatch):
    monkeypatch.setattr("surogates.config.load_settings", lambda: SimpleNamespace(platform_api_url="", llm=SimpleNamespace()))
    store = AsyncMock()
    result = await _service(store).consult(expert=_expert(), task="review", client=_client("Check row 4."))
    assert result.success is True and result.content == "Check row 4."
    assert [c.args[1] for c in store.emit_event.await_args_list][0] == EventType.EXPERT_DELEGATION
    assert EventType.EXPERT_FAILURE not in [c.args[1] for c in store.emit_event.await_args_list]
