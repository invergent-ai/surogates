"""Wiring: the github tool is registered, harness-located, and gets its kwargs."""

from __future__ import annotations

import inspect

from surogates.tools.registry import ToolRegistry
from surogates.tools.runtime import ToolRuntime


def test_github_tool_registered_and_harness_located():
    registry = ToolRegistry()
    ToolRuntime(registry).register_builtins()
    assert registry.has("github")

    from surogates.tools.router import TOOL_LOCATIONS, ToolLocation

    # Default location is SANDBOX; an unlisted tool fails as "Unknown tool", so
    # an explicit HARNESS entry is required for a worker-local tool.
    assert TOOL_LOCATIONS.get("github") == ToolLocation.HARNESS


def test_dispatch_forwards_session_config_and_vault():
    # The handler resolves the PAT from credential_vault and the configured
    # repos from session_config — both must be threaded by the harness dispatch.
    import surogates.harness.tool_exec as te

    src = inspect.getsource(te)
    assert "credential_vault=" in src
    assert "session_config=" in src
