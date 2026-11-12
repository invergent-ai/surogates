"""An agent's policy over the agent family, as a wake gets it: built for the session it governs.

A sub-agent whose policy denies ``send_message`` still reaches the agent that
started it, and nobody else.  A session whose agents run still lists and
stops them under a policy that denies the family, and starts no more.
"""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from surogates.harness.subagents import kept_family_gate
from surogates.runtime.governance import build_governance_gate
from surogates.session.events import EventType
from tests.test_steer_loop import _final_response

from .test_agent_tool import a_chat, calling, chats, tool_result  # noqa: F401  (chats is a fixture)
from .test_command_wake_once import workers  # noqa: F401  (a fixture)
from .test_devices import api  # noqa: F401  (a fixture)
from .test_send_message import a_background_agent

pytestmark = pytest.mark.asyncio(loop_scope="session")


def governed(chats, governance: dict) -> None:  # noqa: F811
    """Every wake from now on runs under *governance*, its gate built for the session it wakes, as the worker builds it."""
    build = chats.worker

    def worker(*args, **kwargs):
        harness = build(*args, **kwargs)
        wake = harness.wake

        async def gated(session_id):
            session = await chats.store.get_session(session_id)
            harness._governance_gate = await kept_family_gate(build_governance_gate(governance), session, chats.store)
            return await wake(session_id)

        harness.wake = gated
        return harness

    chats.worker = worker


async def test_a_sub_agent_whose_policy_denies_send_message_reaches_only_its_parent(chats):  # noqa: F811
    governed(chats, {"enabled": True, "denied_tools": ["send_message"]})
    chat = await a_chat(chats)
    child = await a_background_agent(chats, chat)
    chats.replies = [
        calling("agent", "call_closed", description="Count closed items", prompt="Count them.", run_in_background=True),
        _final_response("Started."),
    ]
    await chats.says(chat, "Count the closed ones too.")
    await chats.wake(chat)
    sibling = UUID(json.loads(await tool_result(chats, chat, "call_closed"))["agent_id"])

    chats.replies = [
        calling("send_message", "call_side", to=str(sibling), message="Send me the closed items.", summary="Closed"),
        calling("send_message", "call_up", to="parent", message="Which tracker?", summary="Which tracker"),
        _final_response("Asked."),
    ]
    await chats.wake(child.id)
    assert "send_message" in chats.offered[-1]
    assert json.loads(await tool_result(chats, child.id, "call_side")) == {
        "error": "you may message only the agent that started you",
    }
    assert await chats.store.get_events(sibling, types=[EventType.COORDINATOR_MESSAGE]) == []
    assert json.loads(await tool_result(chats, child.id, "call_up"))["status"] in ("delivered", "resumed", "queued")


async def test_a_session_whose_agents_run_lists_and_stops_them_under_a_policy_that_denies_the_family(chats):  # noqa: F811
    chat = await a_chat(chats)
    child = await a_background_agent(chats, chat)
    governed(chats, {"enabled": True, "denied_tools": ["agent", "send_message", "list_agents", "stop_agent"]})
    chats.replies = [
        calling("list_agents", "call_list"),
        calling("stop_agent", "call_stop", agent_id=str(child.id)),
        calling("agent", "call_more", description="Count more", prompt="Count more.", run_in_background=True),
        _final_response("Stopped it."),
    ]
    await chats.says(chat, "Stop counting.")
    await chats.wake(chat)
    offered = set(chats.offered[-1])
    assert {"send_message", "list_agents", "stop_agent"} <= offered and "agent" not in offered
    assert [a["agent_id"] for a in json.loads(await tool_result(chats, chat, "call_list"))["agents"]] == [str(child.id)]
    assert json.loads(await tool_result(chats, chat, "call_stop"))["status"] == "stopped"
    assert "error" in json.loads(await tool_result(chats, chat, "call_more"))
