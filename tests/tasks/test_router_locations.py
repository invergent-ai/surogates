"""Regression: every task-layer tool must route to the HARNESS, not the sandbox.

These tools mutate the worker's tasks/sessions tables and enqueue on
redis — none of that is reachable from a sandbox pod. The router's
default for unmapped names is ``SANDBOX``, so a missing entry surfaces
to the LLM as ``Unknown tool`` from the sandbox executor with the
underlying handler never running.
"""
from __future__ import annotations

import pytest

from surogates.tools.router import TOOL_LOCATIONS, ToolLocation


_TASK_TOOLS = (
    "spawn_task",
    "worker_block",
    "worker_complete",
    "worker_context",
    "cancel_task",
    "unblock_task",
)


@pytest.mark.parametrize("tool_name", _TASK_TOOLS)
def test_task_tool_routes_to_harness(tool_name: str) -> None:
    assert TOOL_LOCATIONS.get(tool_name) is ToolLocation.HARNESS, (
        f"{tool_name} must be in TOOL_LOCATIONS as HARNESS; the default "
        "SANDBOX fallback routes the call to a sandbox pod that has no "
        "DB/redis access and returns 'Unknown tool'."
    )


def test_resolve_location_for_task_tools() -> None:
    """End-to-end check via the public resolver method on ToolRouter."""
    from unittest.mock import MagicMock

    from surogates.tools.router import ToolRouter

    router = ToolRouter(
        registry=MagicMock(),
        sandbox_pool=MagicMock(),
        governance=MagicMock(),
    )
    for name in _TASK_TOOLS:
        assert router.resolve_location(name) is ToolLocation.HARNESS, name
