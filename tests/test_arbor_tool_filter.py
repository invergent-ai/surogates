"""Read-only OBSERVE carve-out: research coordinators get read tools back,
but writes/terminal/web stay stripped and user-excluded reads stay excluded.
"""
from __future__ import annotations

from surogates.tools.registry import ToolRegistry, ToolSchema

from tests.test_harness_resilience import _make_harness, _session_with_config

_READ_TOOLS = {"read_file", "search_files", "list_files"}
_STILL_STRIPPED = {"terminal", "write_file", "patch", "web_search", "create_artifact"}
_ALL = _READ_TOOLS | _STILL_STRIPPED | {"idea_tree"}


def _registry_with(names) -> ToolRegistry:
    reg = ToolRegistry()
    for name in names:
        reg.register(
            name, ToolSchema(name=name, description="t", parameters={}),
            lambda _a, **_k: "{}",
        )
    return reg


def test_research_coordinator_restores_reads_only():
    harness = _make_harness(tool_registry=_registry_with(_ALL))
    session = _session_with_config({
        "coordinator": True, "strict_coordinator": True,
        "active_research_run_id": "r1",
    })
    allowed = harness._tool_filter_for_session(session)
    assert _READ_TOOLS <= allowed          # reads restored for OBSERVE
    assert not (_STILL_STRIPPED & allowed)  # writes/terminal/web stay gone
    assert "idea_tree" in allowed           # research tools present


def test_plain_strict_coordinator_keeps_reads_stripped():
    harness = _make_harness(tool_registry=_registry_with(_ALL))
    session = _session_with_config({
        "coordinator": True, "strict_coordinator": True,
    })
    allowed = harness._tool_filter_for_session(session)
    assert not (_READ_TOOLS & allowed)      # no research run -> no carve-out


def test_user_excluded_read_stays_excluded():
    harness = _make_harness(tool_registry=_registry_with(_ALL))
    session = _session_with_config({
        "coordinator": True, "strict_coordinator": True,
        "active_research_run_id": "r1",
        "excluded_tools": ["read_file"],
    })
    allowed = harness._tool_filter_for_session(session)
    assert "read_file" not in allowed       # explicit user exclusion wins
    assert {"search_files", "list_files"} <= allowed
