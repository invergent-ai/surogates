"""What links a parent's and its sub-agents' trajectories into one episode, for training on them.

The tree names, for each sub-agent the ``agent`` tool started, the parent's
tool call that started it.  The parent's log has that call and its
``delegation.start`` (the child's id with the call's id), the end as an
``agent.notification`` (the child's id with the call's id), and each message
between them as a ``coordinator.message`` naming its sender and recipient.
"""

from __future__ import annotations

import pytest

from surogates.session.events import EventType
from tests.test_steer_loop import _final_response

from .test_agent_tool import PARITY, a_chat, calling, chats, children  # noqa: F401  (chats is a fixture)
from .test_command_wake_once import workers  # noqa: F401  (a fixture)
from .test_devices import api  # noqa: F401  (a fixture)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_the_tree_and_the_logs_link_a_parent_and_its_agent_by_the_call_that_started_it(chats):  # noqa: F811
    chat = await a_chat(chats, PARITY)
    chats.replies = [
        calling("agent", "call_count", description="Count open items", prompt="Count the open items in the tracker.",
                run_in_background=True),
        _final_response("Started."),
    ]
    await chats.says(chat, "How many open items?")
    await chats.wake(chat)
    [child] = await children(chats, chat)
    chats.replies = [
        calling("send_message", "call_ask", to="parent", message="Open or closed ones?", summary="Which items"),
        _final_response("12 open items."),
    ]
    await chats.wake(child.id)

    tree = await chats.api.client.get(f"/v1/sessions/{child.id}/tree", headers=chats.api.auth())
    assert tree.status_code == 200, tree.text
    nodes = {node["id"]: node for node in tree.json()["nodes"]}
    assert nodes[str(chat)]["dispatch"] is None
    assert nodes[str(child.id)]["parent_id"] == str(chat)
    assert nodes[str(child.id)]["dispatch"] == {
        "tool_call_id": "call_count", "description": "Count open items", "mode": "background", "fork": False,
        "budget_tokens": None,
    }
    listed = await chats.api.client.get(f"/v1/sessions/{chat}/children", headers=chats.api.auth())
    assert [c["dispatch"]["tool_call_id"] for c in listed.json()["children"]] == ["call_count"]

    [call] = [e.data for e in await chats.store.get_events(chat, types=[EventType.TOOL_CALL])]
    [start] = [e.data for e in await chats.store.get_events(chat, types=[EventType.DELEGATION_START])]
    [message] = [e.data for e in await chats.store.get_events(chat, types=[EventType.COORDINATOR_MESSAGE])]
    [end] = [e.data for e in await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])]
    assert call["tool_call_id"] == start["tool_call_id"] == end["tool_call_id"] == "call_count"
    assert start["child_session_id"] == end["agent_id"] == str(child.id)
    assert (message["from_agent"], message["to_agent"], message["summary"]) == (str(child.id), str(chat), "Which items")
