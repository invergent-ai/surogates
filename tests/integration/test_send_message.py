"""``send_message``: an agent continues, corrects or answers another agent of its own tree.

A running agent reads the message before its next step; one at rest is
resumed with its whole context and the message, and its new report reaches
the agent that started it as a notification.  A message never reaches an
agent outside the sender's tree, nor another organisation's.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from surogates.harness.subagents import DISPATCH_KEY, PARITY_KEY
from surogates.session.events import EventType
from surogates.session.provisioning import create_child_session
from surogates.tools.builtin import agents as agents_module
from tests.test_steer_loop import _final_response

from .conftest import create_org
from .test_agent_tool import PARITY, a_chat, calling, chats, children, tool_result  # noqa: F401  (chats is a fixture)
from .test_command_wake_once import workers  # noqa: F401  (a fixture)
from .test_devices import api  # noqa: F401  (a fixture)
from .test_workstream_threads import TODO_CALL, queued

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def a_background_agent(chats, chat, description="Count open items"):  # noqa: F811
    chats.replies = [
        calling("agent", "call_agent", description=description, prompt="Count the open items in the tracker.",
                run_in_background=True),
        _final_response("Started."),
    ]
    await chats.says(chat, "Count the open items.")
    await chats.wake(chat)
    [child] = await children(chats, chat)
    return child


async def send(chats, sender, to, message="Also count the closed ones.", summary="Count closed ones too"):  # noqa: F811
    return json.loads(await agents_module._send_message_handler(
        {"to": str(to), "message": message, "summary": summary},
        session_store=chats.store, session_id=str(sender), redis=chats.api.app.state.redis,
    ))


async def test_a_running_agent_reads_a_message_before_its_next_step(chats):  # noqa: F811
    chat = await a_chat(chats, PARITY)
    child = await a_background_agent(chats, chat)
    sent: list[dict] = []

    async def the_parent_writes():
        sent.append(await send(chats, chat, child.id))

    chats.during_the_tool_call = the_parent_writes
    chats.replies = [TODO_CALL, _final_response("12 open, 4 closed.")]
    await chats.wake(child.id)
    assert sent == [{"status": "delivered", "to": str(child.id), "note": "It reads the message before its next step."}]
    second = chats.requests[-1]
    assert second[-2]["role"] == "tool"
    assert second[-1] == {
        "role": "user",
        "content": "[Message from the agent that started you: Count closed ones too]\n```\nAlso count the closed ones.\n```",
    }
    [message] = await chats.store.get_events(child.id, types=[EventType.COORDINATOR_MESSAGE])
    assert message.data["kind"] == "agent_message"
    assert (message.data["from_agent"], message.data["to_agent"]) == (str(chat), str(child.id))


async def test_a_finished_agent_resumes_with_its_context_and_reports_again_as_a_notification(chats):  # noqa: F811
    chat = await a_chat(chats, PARITY)
    child = await a_background_agent(chats, chat)
    chats.replies = [_final_response("There are 12 open items.")]
    await chats.wake(child.id)
    first_run = chats.requests[-1]
    chats.replies = [_final_response("Noted.")]
    await chats.wake(chat)

    chats.replies = [
        calling("send_message", "call_more", to=str(child.id), message="Now count the closed ones.", summary="Count closed ones"),
        _final_response("Asked it."),
    ]
    await chats.says(chat, "And the closed ones?")
    await chats.wake(chat)
    assert json.loads(await tool_result(chats, chat, "call_more"))["status"] == "resumed"
    assert await chats.status(child.id) == "active"
    assert await queued(chats.api, await chats.session(child.id))

    chats.replies = [_final_response("There are 4 closed items.")]
    await chats.wake(child.id)
    resumed = chats.requests[-1]
    # Its earlier run, its answer, then the message.
    assert resumed[: len(first_run)] == first_run
    assert resumed[-1] == {
        "role": "user", "content": "[Message from the agent that started you: Count closed ones]\n```\nNow count the closed ones.\n```",
    }
    notes = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    assert [n.data["result"] for n in notes] == ["There are 12 open items.", "There are 4 closed items."]


async def test_a_foreground_agent_continued_by_a_message_reports_as_a_notification(chats):  # noqa: F811
    chat = await a_chat(chats, PARITY)
    parent = await chats.session(chat)
    child = await create_child_session(
        store=chats.store, parent=parent, channel="delegation",
        config={PARITY_KEY: True, DISPATCH_KEY: {"description": "Read the tracker", "mode": "foreground", "fork": False,
                                                "tool_call_id": "call_x", "parent": str(chat)}},
    )
    await chats.store.update_session_status(child.id, "completed")
    # The call that started it has returned with its report.
    await chats.store.emit_event(chat, EventType.DELEGATION_COMPLETE, {"child_session_id": str(child.id)})
    assert (await send(chats, chat, child.id))["status"] == "resumed"
    assert (await chats.session(child.id)).config[DISPATCH_KEY]["mode"] == "background"
    chats.replies = [_final_response("4 closed.")]
    await chats.wake(child.id)
    [note] = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    assert note.data["result"] == "4 closed." and note.data["summary"] == "Read the tracker"


async def test_an_agent_messages_the_agent_that_started_it(chats):  # noqa: F811
    chat = await a_chat(chats, PARITY)
    child = await a_background_agent(chats, chat)
    assert await chats.status(chat) == "completed"
    chats.replies = [
        calling("send_message", "call_up", to="parent", message="The tracker has two lists; which one?", summary="Which list"),
        _final_response("Waiting for an answer."),
    ]
    await chats.wake(child.id)
    assert json.loads(await tool_result(chats, child.id, "call_up"))["status"] == "resumed"
    before = len(chats.requests)
    chats.replies = [_final_response("Use the open list.")]
    await chats.wake(chat)
    # Its message, then the notification of its turn's end, which came after it.
    assert chats.requests[before][-2:] == [
        {"role": "user", "content": f"[Message from agent {child.id} (Count open items): Which list]\n```\nThe tracker has two lists; which one?\n```"},
        {"role": "user", "content": f"[Agent {child.id} (Count open items) completed]\nWaiting for an answer."},
    ]


async def test_a_message_reaches_only_the_senders_own_tree_and_organisation(chats):  # noqa: F811
    chat, stranger = await a_chat(chats, PARITY), await a_chat(chats, PARITY)
    first = await a_background_agent(chats, chat, "First")
    chats.replies = [
        calling("agent", "call_two", description="Second", prompt="Count the closed items.", run_in_background=True),
        _final_response("Started."),
    ]
    await chats.says(chat, "And the closed ones.")
    await chats.wake(chat)
    second = next(c for c in await children(chats, chat) if c.id != first.id)
    theirs = await a_background_agent(chats, stranger, "Theirs")
    # A helper delegate_task started is no agent to message: its parent waited for it.
    legacy = await create_child_session(store=chats.store, parent=await chats.session(chat), channel="delegation")
    org = await create_org(chats.api.app.state.session_factory)
    elsewhere = await chats.store.create_session(
        user_id=None, org_id=org, agent_id=first.agent_id, channel="delegation", parent_id=None,
        config={DISPATCH_KEY: {"mode": "background", "description": "x", "parent": str(chat)}},
    )

    assert (await send(chats, first.id, second.id))["status"]  # a sibling
    assert (await send(chats, second.id, "parent"))["status"]  # its parent
    for sender, target in [
        (chat, theirs.id), (first.id, theirs.id), (stranger, first.id), (theirs.id, chat),
        (chat, legacy.id), (chat, elsewhere.id), (chat, uuid4()), (chat, chat),
    ]:
        answer = await send(chats, sender, target)
        assert answer["error"].startswith("no agent "), (sender, target, answer)
    assert await chats.store.get_events(theirs.id, types=[EventType.COORDINATOR_MESSAGE]) == []
    assert await chats.store.get_events(elsewhere.id, types=[EventType.COORDINATOR_MESSAGE]) == []
    # A session started by no agent has no parent to message.
    assert "no parent" in (await send(chats, chat, "parent"))["error"]


async def test_a_sub_agent_never_undoes_its_users_stop_of_its_parent(chats):  # noqa: F811
    chat = await a_chat(chats, PARITY)
    child = await a_background_agent(chats, chat)
    # The user stops the chat (the pause route's write): its agent keeps running.
    await chats.store.emit_event(chat, EventType.SESSION_PAUSE, {}, status="paused")
    answer = await send(chats, child.id, "parent", message="Done counting.", summary="Done")
    assert answer["status"] == "queued" and "its user resumes it" in answer["note"]
    assert await chats.status(chat) == "paused"
    assert not await queued(chats.api, await chats.session(chat))


async def test_a_foreground_agent_whose_call_still_waits_is_not_switched(chats):  # noqa: F811
    chat = await a_chat(chats, PARITY)
    child = await create_child_session(
        store=chats.store, parent=await chats.session(chat), channel="delegation",
        config={PARITY_KEY: True, DISPATCH_KEY: {"description": "Read", "mode": "foreground", "parent": str(chat)}},
    )
    assert (await send(chats, chat, child.id))["status"] == "delivered"
    assert (await chats.session(child.id)).config[DISPATCH_KEY]["mode"] == "foreground"


async def test_only_an_agent_of_the_family_speaks_up_to_its_parent(chats):  # noqa: F811
    chat = await a_chat(chats, PARITY)
    worker = await create_child_session(store=chats.store, parent=await chats.session(chat), channel="worker",
                                        config={PARITY_KEY: True})
    assert (await send(chats, worker.id, "parent"))["error"].startswith("no agent ")


async def test_a_sub_agent_does_not_resume_its_own_finished_agent(chats):  # noqa: F811
    chat = await a_chat(chats, PARITY)
    middle = await create_child_session(
        store=chats.store, parent=await chats.session(chat), channel="delegation",
        config={PARITY_KEY: True, DISPATCH_KEY: {"description": "Lead", "mode": "foreground", "parent": str(chat)}},
    )
    leaf = await create_child_session(
        store=chats.store, parent=middle, channel="delegation",
        config={PARITY_KEY: True, DISPATCH_KEY: {"description": "Read", "mode": "foreground", "parent": str(middle.id)}},
    )
    await chats.store.update_session_status(leaf.id, "completed")
    answer = await send(chats, middle.id, leaf.id)
    assert answer["status"] == "queued" and await chats.status(leaf.id) == "completed"
