"""Agents of the ``agent`` family addressed by name: started with one, then messaged, stopped and listed by it."""

from __future__ import annotations

import json

import pytest

from surogates.harness.agent_news import notification_note
from surogates.harness.subagents import DISPATCH_KEY
from surogates.session.events import EventType
from tests.test_steer_loop import _final_response

from .test_agent_tool import a_chat, calling, chats, children, tool_result  # noqa: F401  (chats is a fixture)
from .test_command_wake_once import workers  # noqa: F401  (a fixture)
from .test_devices import api  # noqa: F401  (a fixture)

pytestmark = pytest.mark.asyncio(loop_scope="session")


def agent_call(call_id: str, **arguments) -> tuple[dict, dict]:
    """An ``agent`` call: built here, since ``calling`` takes the tool's name as its own ``name``."""
    return (
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": "agent", "arguments": json.dumps(arguments)}},
        ]},
        {"model": "test-model", "finish_reason": "tool_calls", "input_tokens": 1, "output_tokens": 1},
    )


def named(children_, name: str):
    return [child for child in children_ if (child.config.get(DISPATCH_KEY) or {}).get("name") == name]


async def test_an_agent_started_with_a_name_is_messaged_stopped_and_listed_by_it(chats):  # noqa: F811
    chat = await a_chat(chats)
    chats.replies = [
        agent_call("call_a", description="Count open items", prompt="Count them.", name="counter",
                run_in_background=True),
        calling("send_message", "call_m", to="counter", message="Also count closed ones.", summary="Closed too"),
        calling("list_agents", "call_l"),
        calling("stop_agent", "call_s", agent_id="counter"),
        _final_response("Done."),
    ]
    await chats.says(chat, "Count, then stop.")
    await chats.wake(chat)
    [child] = await children(chats, chat)
    assert child.config[DISPATCH_KEY]["name"] == "counter"
    assert json.loads(await tool_result(chats, chat, "call_m"))["to"] == str(child.id)
    [listed] = json.loads(await tool_result(chats, chat, "call_l"))["agents"]
    assert listed["name"] == "counter" and listed["agent_id"] == str(child.id)
    assert json.loads(await tool_result(chats, chat, "call_s"))["status"] == "stopped"


async def test_the_newest_agent_with_a_name_answers_to_it(chats):  # noqa: F811
    chat = await a_chat(chats)
    for call in ("call_a", "call_b"):
        chats.replies = [
            agent_call(call, description="Count open items", prompt="Count them.", name="counter",
                    run_in_background=True),
            _final_response("Started."),
        ]
        await chats.says(chat, "Count them.")
        await chats.wake(chat)
    first = json.loads(await tool_result(chats, chat, "call_a"))["agent_id"]
    second = json.loads(await tool_result(chats, chat, "call_b"))["agent_id"]
    chats.replies = [
        calling("send_message", "call_name", to="counter", message="Hurry.", summary="Hurry"),
        calling("send_message", "call_id", to=first, message="You too.", summary="You too"),
        _final_response("Sent."),
    ]
    await chats.says(chat, "Hurry them.")
    await chats.wake(chat)
    assert json.loads(await tool_result(chats, chat, "call_name"))["to"] == second
    assert json.loads(await tool_result(chats, chat, "call_id"))["to"] == first


@pytest.mark.parametrize("name", ["parent", "Parent", "a9f1c2d6-0000-4000-8000-000000000000", "proc_1", "1st", "a b", "x" * 41])
async def test_a_name_that_could_be_read_as_something_else_is_refused(chats, name):  # noqa: F811
    chat = await a_chat(chats)
    chats.replies = [
        agent_call("call_a", description="Count", prompt="Count them.", name=name, run_in_background=True),
        _final_response("No."),
    ]
    await chats.says(chat, "Count them.")
    await chats.wake(chat)
    assert "name must start with a letter" in json.loads(await tool_result(chats, chat, "call_a"))["error"]
    assert await children(chats, chat) == []


async def test_a_sub_agent_messages_a_sibling_by_its_name(chats):  # noqa: F811
    chat = await a_chat(chats)
    chats.replies = [
        agent_call("call_r", description="Read the tracker", prompt="Read it.", name="reader", run_in_background=True),
        agent_call("call_w", description="Write the summary", prompt="Write it.", name="writer", run_in_background=True),
        _final_response("Started."),
    ]
    await chats.says(chat, "Read, then write.")
    await chats.wake(chat)
    found = await children(chats, chat)
    [reader], [writer] = named(found, "reader"), named(found, "writer")
    chats.replies = [
        calling("send_message", "call_side", to="reader", message="Send me the open items.", summary="Open items"),
        _final_response("Asked."),
    ]
    await chats.wake(writer.id)
    assert json.loads(await tool_result(chats, writer.id, "call_side"))["to"] == str(reader.id)
    [message] = await chats.store.get_events(reader.id, types=[EventType.COORDINATOR_MESSAGE])
    assert message.data["from_agent"] == str(writer.id)


async def test_a_notification_carries_the_agents_name(chats):  # noqa: F811
    chat = await a_chat(chats)
    chats.replies = [
        agent_call("call_a", description="Count open items", prompt="Count them.", name="counter",
                run_in_background=True),
        _final_response("Started."),
    ]
    await chats.says(chat, "Count them.")
    await chats.wake(chat)
    [child] = await children(chats, chat)
    chats.replies = [_final_response("There are 12.")]
    await chats.wake(child.id)
    [note] = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    assert note.data["name"] == "counter"
    assert (
        f'<summary>Agent counter ({child.id}, "Count open items") finished</summary>'
        in notification_note(note.data)["content"]
    )
