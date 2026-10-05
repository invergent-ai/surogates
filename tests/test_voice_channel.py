"""A voice session is spoken to, and its memory stays inside the boundary the call chose."""
from types import SimpleNamespace

from surogates.channels.memory_boundary import session_memory_boundary
from surogates.harness.prompt_library import default_library


def test_voice_sessions_get_a_spoken_platform_hint():
    hint = default_library().platform_hint("voice")
    assert hint and "telefon" in hint.lower() and "markdown" in hint.lower()


def test_a_voice_call_keeps_its_own_memory_boundary():
    call = SimpleNamespace(channel="voice", config={"memory_boundary": "voice:call:SCL_1"}, id="s1")
    assert session_memory_boundary(call) == "voice:call:SCL_1"
    remembered = SimpleNamespace(channel="voice", config={"memory_boundary": "phone:40722000111"}, id="s2")
    assert session_memory_boundary(remembered) == "phone:40722000111"


async def test_a_spoken_answer_is_never_judged_into_an_inbox_rescue():
    """The final-response judge turns text answers into ask_user_question / inbox items. On a call the caller
    has already heard the answer, and up to three blocking model calls would hold their next turn."""
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from surogates.harness.loop import AgentHarness

    judge = AsyncMock(return_value={"action_kind": "ask_user_question"})
    harness = SimpleNamespace(_judge_final_response_user_action=judge)
    session = SimpleNamespace(channel="voice", user_id=uuid4(), service_account_id=None, parent_id=None, id=uuid4())
    out = await AgentHarness._maybe_route_final_response_to_inbox(
        harness, session=session, messages=[], assistant_message={"role": "assistant", "content": "Euro e 4,97 lei."},
        model="m", tool_filter=None)
    assert out is None and judge.await_count == 0


def _schema(name):
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object", "properties": {}}}}


def test_a_call_gets_end_call_and_none_of_the_text_channels_tools():
    """voice is a managed channel for memory, but Slack's message/file tools mean nothing on a phone."""
    from surogates.harness.tool_schemas import channel_tool_flags, drop_unusable_tools

    names = ["end_call", "fetch_channel_messages", "fetch_channel_file", "web_search"]
    kept = drop_unusable_tools([_schema(n) for n in names], has_kbs=True, is_scheduled=False, **channel_tool_flags("voice"))
    assert [s["function"]["name"] for s in kept] == ["end_call", "web_search"]
    kept = drop_unusable_tools([_schema(n) for n in names], has_kbs=True, is_scheduled=False, **channel_tool_flags("slack"))
    assert [s["function"]["name"] for s in kept] == ["fetch_channel_messages", "fetch_channel_file", "web_search"]


async def test_end_call_tells_the_agent_to_say_goodbye():
    import json

    from surogates.tools.builtin.voice_call import _end_call_handler

    out = json.loads(await _end_call_handler({"reason": "caller is done"}))
    assert out["success"] is True and "goodbye" in out["message"].lower()
