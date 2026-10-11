"""An agent with browser support off is offered no browser tool.

The toggle only took the browser tools out of the prompt's tool list.  The
schemas the model is sent are bounded by the harness's tool filter, which
never heard of it, so a new agent with the browser off still listed all 12
browser tools and could call them.
"""

from __future__ import annotations

from surogates.orchestrator.worker import _entitlement_tool_exclusions
from surogates.tools.registry import ToolRegistry
from surogates.tools.runtime import ToolRuntime
from tests.test_steer_loop import _make_loop_harness, _make_session


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    ToolRuntime(registry).register_builtins()
    return registry


def _browser_tools(registry: ToolRegistry) -> set[str]:
    return {e.name for e in registry.get_all() if e.toolset == "browser"}


def _exclusions(registry: ToolRegistry, *, browser_enabled: bool) -> set[str]:
    return _entitlement_tool_exclusions(
        session_config={}, tool_registry=registry, mcp_scope=None,
        browser_enabled=browser_enabled,
    )


def test_an_agent_with_the_browser_off_excludes_every_browser_tool() -> None:
    registry = _registry()
    assert len(_browser_tools(registry)) == 12
    assert _browser_tools(registry) <= _exclusions(registry, browser_enabled=False)


def test_an_agent_with_the_browser_on_keeps_its_browser_tools() -> None:
    registry = _registry()
    assert _browser_tools(registry).isdisjoint(_exclusions(registry, browser_enabled=True))


def test_the_model_is_offered_no_browser_tool_when_the_browser_is_off() -> None:
    registry = _registry()
    harness = _make_loop_harness(session_store=None)
    harness._tools = registry
    # What the worker hands the harness for this agent.
    harness._entitlement_excluded_tools = frozenset(
        _exclusions(registry, browser_enabled=False),
    )

    offered = harness._tool_filter_for_session(_make_session())

    assert offered is not None
    assert offered.isdisjoint(_browser_tools(registry))
