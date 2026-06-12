"""Routing + registration + visibility regression for the arbor tools.

Platform rule: a builtin tool missing from ``TOOL_LOCATIONS`` silently
routes to the sandbox executor and fails as "Unknown tool".
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

from surogates.orchestrator.worker import _filter_effective_tools
from surogates.tenant.context import TenantContext
from surogates.tools.router import TOOL_LOCATIONS, ToolLocation

ARBOR_TOOLS = ("idea_tree", "dispatch_experiments", "merge_experiment")


def test_arbor_tools_route_to_harness():
    for name in ARBOR_TOOLS:
        assert TOOL_LOCATIONS.get(name) == ToolLocation.HARNESS, (
            f"{name} must have an explicit HARNESS entry in TOOL_LOCATIONS"
        )


def test_arbor_tools_register():
    from surogates.tools.builtin import arbor

    registered: list[str] = []

    class FakeRegistry:
        def register(self, *, name, schema, handler, toolset="core", **kw):
            registered.append(name)

    arbor.register(FakeRegistry())
    assert sorted(registered) == sorted(ARBOR_TOOLS)


def _tenant() -> TenantContext:
    return TenantContext(
        org_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root="/tmp/assets",
    )


def _session(*, config: dict, task_id=None):
    return SimpleNamespace(
        config=config, task_id=task_id,
        service_account_id=None, channel="web",
    )


def _filter(tools, *, config, task_id=None):
    return _filter_effective_tools(
        tools=set(tools), tenant=_tenant(),
        session=_session(config=config, task_id=task_id),
        use_api_for_harness_tools=True,
    )


def test_arbor_tools_hidden_without_active_run():
    tools = list(ARBOR_TOOLS) + ["spawn_task"]

    # Coordinator session WITHOUT a research run: arbor tools stripped.
    out = _filter(tools, config={"coordinator": True})
    assert not set(ARBOR_TOOLS) & out
    assert "spawn_task" in out

    # Research coordinator (run active, not a task worker): tools visible.
    out = _filter(tools, config={"coordinator": True, "active_research_run_id": "r1"})
    assert set(ARBOR_TOOLS) <= out

    # Task workers NEVER see them (executors stay tree-blind), even with
    # the run id inherited in config.
    out = _filter(tools, config={"active_research_run_id": "r1"}, task_id="t1")
    assert not set(ARBOR_TOOLS) & out
