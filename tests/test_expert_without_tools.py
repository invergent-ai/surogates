"""An expert sent no tools is told so, and a consult that returns no text is not a success."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.session.events import EventType
from surogates.tools.builtin.expert_loop import (
    ExpertScope,
    _build_expert_system_prompt,
    expert_scope,
    run_expert_loop,
)
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


def test_an_expert_with_tools_reads_exactly_what_it_read_before():
    assert _build_expert_system_prompt(_expert(["terminal", "read_file"]), ["terminal", "read_file"]) == (
        "You are an expert assistant specialised in: Reviews work\n\n"
        f"{WITH_TOOLS}\n"
        "\nYou have access to these tools: terminal, read_file. Do not attempt to call tools not in this list.\n"
        "\n# Expert Instructions\nExpert body."
    )


@pytest.mark.parametrize("registered, withheld", [(False, frozenset()), (True, frozenset({"terminal"}))])
async def test_the_loop_says_no_tools_when_the_request_holds_none(registered, withheld, monkeypatch):
    """Its one tool is unregistered, or the consulting session may not send it."""
    monkeypatch.setattr("surogates.config.load_settings", lambda: SimpleNamespace(platform_api_url="", llm=SimpleNamespace()))
    registry = MagicMock()
    registry.get_schemas.side_effect = lambda names: (
        [{"function": {"name": n}} for n in names] if registered else []
    )
    client = _client("Check row 4.")
    token = expert_scope.set(ExpertScope(withheld=withheld))
    try:
        await run_expert_loop(
            expert=_expert(["terminal"]), task="t", context=None, tool_router=MagicMock(), tool_registry=registry,
            tenant=SimpleNamespace(org_config={}), session_id=uuid4(), client=client,
        )
    finally:
        expert_scope.reset(token)
    sent = client.chat.completions.create.await_args.kwargs
    assert "tools" not in sent
    assert "You have no tools here" in sent["messages"][0]["content"]
    assert "You have access" not in sent["messages"][0]["content"]


def _service(store) -> ExpertConsultationService:
    return ExpertConsultationService(
        tenant=SimpleNamespace(org_id=uuid4(), user_id=uuid4(), org_config={}),
        session_id=uuid4(), tool_registry=MagicMock(), session_store=store,
    )


def _client(content, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=message)]))
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


@pytest.mark.parametrize("content", [
    "", "   \n", None, "<think>only reasoning</think>", "<thinking>cut off", "<THINK>x</THINK>",
    "plan only</think>", "<REASONING_SCRATCHPAD>x</REASONING_SCRATCHPAD>", "<REASONING_SCRATCHPAD>x",
])
async def test_a_consult_with_no_text_is_a_failure_the_caller_reads(content, monkeypatch):
    monkeypatch.setattr("surogates.config.load_settings", lambda: SimpleNamespace(platform_api_url="", llm=SimpleNamespace()))
    store = AsyncMock()
    result = await _service(store).consult(expert=_expert(), task="review", client=_client(content))
    assert result.success is False
    assert "returned no text" in result.content and "proceed without" in result.content
    assert "ran tools" not in result.content
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


def _call(name):
    return SimpleNamespace(id="call-1", function=SimpleNamespace(name=name, arguments="{}"))


def _replies(*messages):
    create = AsyncMock(side_effect=[
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=c, tool_calls=t))])
        for c, t in messages
    ])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _with_tools(store, registered, router):
    registry = MagicMock()
    registry.get_schemas.side_effect = lambda names: (
        [{"function": {"name": n}} for n in names] if registered else []
    )
    return ExpertConsultationService(
        tenant=SimpleNamespace(org_id=uuid4(), user_id=uuid4(), org_config={}),
        session_id=uuid4(), tool_registry=registry, session_store=store, tool_router=router,
    )


def _two_rounds(tools):
    expert = _expert(tools)
    expert.expert_max_iterations = 2
    return expert


async def test_a_consult_that_ran_a_tool_and_then_said_nothing_warns_of_its_effects(monkeypatch):
    monkeypatch.setattr("surogates.config.load_settings", lambda: SimpleNamespace(platform_api_url="", llm=SimpleNamespace()))
    router = SimpleNamespace(execute=AsyncMock(return_value="ok"))
    result = await _with_tools(AsyncMock(), True, router).consult(
        expert=_two_rounds(["terminal"]), task="fix it",
        client=_replies((None, [_call("terminal")]), ("", None)),
    )
    router.execute.assert_awaited_once()
    assert result.success is False and "ran tools" in result.content and "before repeating" in result.content


async def test_a_refused_call_is_not_reported_as_a_tool_that_ran(monkeypatch):
    monkeypatch.setattr("surogates.config.load_settings", lambda: SimpleNamespace(platform_api_url="", llm=SimpleNamespace()))
    router = SimpleNamespace(execute=AsyncMock(return_value="ok"))
    result = await _with_tools(AsyncMock(), True, router).consult(
        expert=_two_rounds(None), task="review",
        client=_replies((None, [_call("read_file")]), ("", None)),
    )
    router.execute.assert_not_awaited()
    assert result.success is False and result.iterations_used == 2
    assert "ran tools" not in result.content


async def test_a_declared_tool_no_one_registered_cannot_be_called(monkeypatch):
    monkeypatch.setattr("surogates.config.load_settings", lambda: SimpleNamespace(platform_api_url="", llm=SimpleNamespace()))
    router = SimpleNamespace(execute=AsyncMock(return_value="ok"))
    client = _replies((None, [_call("terminal")]), ("Check row 4.", None))
    result = await _with_tools(AsyncMock(), False, router).consult(
        expert=_two_rounds(["terminal"]), task="review", client=client,
    )
    router.execute.assert_not_awaited()
    assert result.success is True
    sent = client.chat.completions.create.await_args_list[1].kwargs["messages"]
    assert "It has no tools." in sent[-1]["content"]


async def test_text_beside_reasoning_is_an_answer(monkeypatch):
    store = AsyncMock()
    service = _service(store)
    monkeypatch.setattr(
        "surogates.tools.builtin.expert_service.run_expert_loop",
        AsyncMock(return_value=("<think>weigh it</think>Check row 4.", 1)),
    )
    result = await service.consult(expert=_expert(), task="review", client=_client(""))
    assert result.success is True
