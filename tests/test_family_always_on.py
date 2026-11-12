"""The ``agent`` family is the one way to start sub-agents: always on, no flag, no ``delegate_task``."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.harness.agent_news import NEW_FORMAT
from surogates.harness.subagents import DISPATCH_KEY, offer_family, tell_parent
from surogates.session.events import EventType
from surogates.tools.registry import ToolRegistry
from surogates.tools.router import TOOL_LOCATIONS
from surogates.tools.runtime import ToolRuntime

FAMILY = {"agent", "send_message", "list_agents", "stop_agent", "monitor"}


def _builtins() -> set[str]:
    registry = ToolRegistry()
    ToolRuntime(registry).register_builtins()
    return set(registry.tool_names)


def test_a_session_with_no_config_is_offered_the_family():
    assert FAMILY <= offer_family({"agent", "terminal", "read_file"}, {}, root=True)


def test_a_session_key_no_longer_switches_the_family_off():
    tools = offer_family({"agent", "terminal"}, {"subagent_parity": False}, root=True)
    assert {"agent", "send_message"} <= tools


def test_delegate_task_is_no_tool():
    assert "delegate_task" not in _builtins()
    assert "delegate_task" not in TOOL_LOCATIONS


def test_the_coordinator_worker_tools_are_no_tools():
    old = {"spawn_worker", "send_worker_message", "stop_worker"}
    assert not old & _builtins()
    assert not old & set(TOOL_LOCATIONS)


class _Store:
    """What ``tell_parent`` reads and writes, in memory."""

    def __init__(self) -> None:
        self.written: list[tuple] = []

    async def get_events(self, session_id, **_):
        return []

    async def emit_event(self, session_id, event_type, data):
        self.written.append((session_id, event_type, data))

    async def get_session(self, session_id):
        return SimpleNamespace(id=session_id, config={}, org_id=uuid4(), agent_id="a", channel="web")


@pytest.mark.asyncio
async def test_every_new_notification_is_marked():
    store, parent = _Store(), uuid4()
    child = SimpleNamespace(
        id=uuid4(), parent_id=parent, config={DISPATCH_KEY: {"mode": "background", "description": "Read"}},
    )
    await tell_parent(store=store, redis=None, session=child, status="completed", result="done")
    (notification,) = [data for _, kind, data in store.written if kind == EventType.AGENT_NOTIFICATION]
    assert notification["format"] == NEW_FORMAT
