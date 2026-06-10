"""Wiring: the tool is registered, harness-located, and gets the vault kwarg."""

from __future__ import annotations

from surogates.tools.registry import ToolRegistry
from surogates.tools.runtime import ToolRuntime


def test_tool_is_registered_and_harness_located():
    registry = ToolRegistry()
    runtime = ToolRuntime(registry)
    runtime.register_builtins()
    assert registry.has("run_coding_agent")

    from surogates.tools.router import TOOL_LOCATIONS, ToolLocation

    # Worker-local, like consult_expert / delegate_task. Default is SANDBOX,
    # so an explicit HARNESS entry is required.
    assert TOOL_LOCATIONS.get("run_coding_agent") == ToolLocation.HARNESS


def test_dispatch_kwargs_include_credential_vault():
    # The harness dispatch must forward credential_vault so the tool can
    # resolve the user's connected plan.
    import inspect

    import surogates.harness.tool_exec as te

    src = inspect.getsource(te)
    assert "credential_vault=" in src
