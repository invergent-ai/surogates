"""``post_to_channel`` is offered only to a session with a Slack to post to."""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.harness.loop import AgentHarness
from surogates.harness.tool_schemas import drop_unusable_tools, unusable_tool_names
from surogates.harness.tool_groups import tool_groups
from surogates.orchestrator.worker import _filter_effective_tools
from surogates.tools.builtin import channel_post
from surogates.tools.builtin.channel_post import has_slack_connection


class _Ops:
    def __init__(self, routings=(), error=None):
        self.routings, self.error, self.asked = list(routings), error, 0

    async def list_channel_routings(self, kind):
        self.asked += 1
        if self.error is not None:
            raise self.error
        return self.routings if kind == "slack" else []


def _session(channel="web", config=None, agent_id="agent-1", org_id="org-1"):
    return SimpleNamespace(channel=channel, config=config or {}, agent_id=agent_id, org_id=org_id, parent_id=None)


MINE = {"agent_id": "agent-1", "org_id": "org-1", "channel_identifier": "A0APP"}


@pytest.fixture(autouse=True)
def _no_copy_between_tests():
    channel_post._routings_seen.clear()


async def test_an_agent_ops_routes_a_slack_to_has_one():
    assert await has_slack_connection(_session(), _Ops([MINE])) is True


@pytest.mark.parametrize("routing", [
    {"agent_id": "agent-2", "org_id": "org-1", "channel_identifier": "A0APP"},
    {"agent_id": "agent-1", "org_id": "org-2", "channel_identifier": "A0APP"},
    {"agent_id": "agent-1", "org_id": "org-1", "channel_identifier": ""},
])
async def test_someone_elses_or_an_empty_routing_is_no_connection(routing):
    assert await has_slack_connection(_session(), _Ops([routing])) is False


async def test_no_routing_and_no_ops_are_no_connection():
    assert await has_slack_connection(_session(), _Ops()) is False
    assert await has_slack_connection(_session(), None) is False


async def test_a_slack_conversation_has_its_own_without_asking_ops():
    ops = _Ops()
    session = _session(channel="slack", config={"channel_identifier": "A0HERE"})
    assert await has_slack_connection(session, ops) is True
    assert ops.asked == 0


async def test_a_package_without_slack_has_none_whatever_ops_routes():
    session = _session(config={"entitlements": {"channels": ["whatsapp"]}})
    assert await has_slack_connection(session, _Ops([MINE])) is False


async def test_a_failed_lookup_keeps_the_tool():
    assert await has_slack_connection(_session(), _Ops(error=RuntimeError("ops down"))) is True


async def test_a_failed_lookup_is_not_remembered():
    ops = _Ops(error=RuntimeError("ops down"))
    await has_slack_connection(_session(), ops)
    ops.error, ops.routings = None, []
    assert await has_slack_connection(_session(), ops) is False


async def test_wakes_share_one_answer_from_ops_for_a_short_while(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(channel_post.time, "monotonic", lambda: clock[0])
    ops = _Ops([MINE])
    await has_slack_connection(_session(), ops)
    await has_slack_connection(_session(agent_id="agent-2"), ops)
    assert ops.asked == 1
    clock[0] += channel_post._ROUTINGS_TTL_S
    ops.routings = []
    assert await has_slack_connection(_session(), ops) is False
    assert ops.asked == 2


async def test_a_post_always_asks_ops():
    ops = _Ops([MINE])
    await has_slack_connection(_session(), ops)
    ops.routings = []
    assert await channel_post._slack_app_id(_session(), ops) is None


SCHEMAS = [{"function": {"name": n}} for n in ("post_to_channel", "read_file", "fetch_channel_messages")]


def test_the_request_drops_it_without_a_slack_and_nothing_else_for_it():
    with_it = {s["function"]["name"] for s in drop_unusable_tools(SCHEMAS, has_kbs=True, has_channel=True, makes_routines=True)}
    without = {s["function"]["name"] for s in drop_unusable_tools(
        SCHEMAS, has_kbs=True, has_channel=True, makes_routines=True, has_slack=False,
    )}
    assert with_it - without == {"post_to_channel"}


def test_a_consulted_expert_is_not_sent_it_either():
    facts = dict(has_kbs=True, has_channel=True, makes_routines=True)
    assert "post_to_channel" not in unusable_tool_names(**facts)
    assert "post_to_channel" in unusable_tool_names(**facts, has_slack=False)


@pytest.mark.parametrize("has_slack", [True, False])
def test_the_prompt_surface_follows_the_same_answer(has_slack):
    tenant = SimpleNamespace(user_id=uuid4(), service_account_id=None)
    session = SimpleNamespace(channel="web", config={}, parent_id=None, id=uuid4())
    out = _filter_effective_tools(
        tools={"post_to_channel", "read_file"}, tenant=tenant, session=session,
        use_api_for_harness_tools=True, has_slack=has_slack,
    )
    assert ("post_to_channel" in out) is has_slack
    assert "read_file" in out


def test_a_harness_built_without_the_answer_keeps_the_tool():
    harness = AgentHarness.__new__(AgentHarness)
    assert getattr(harness, "_has_slack", True) is True


def test_no_on_demand_tool_group_brings_it_back():
    assert not any(group.has("post_to_channel") for group in tool_groups())
