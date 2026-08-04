"""A task worker keeps its execution-context self-tools under an
AgentDef ``tools`` allowlist.

The allowlist scopes what a worker may DO; ``worker_complete`` /
``worker_block`` / ``worker_context`` are how it reports doing it, and
the board tools are how a coordination-group member shares notes.  When
the schemas dropped them, a worker that could not proceed had no way to
signal failure and its refusal was filed as a successful result.
"""
from __future__ import annotations

from uuid import uuid4

from surogates.runtime.governance import BOARD_SELF_TOOLS, WORKER_SELF_TOOLS
from surogates.tools.registry import ToolRegistry, ToolSchema

from tests.test_harness_resilience import _make_harness, _session_with_config

_ALLOWED = {"run_coding_agent", "read_file", "list_files", "search_files"}
_ALL = _ALLOWED | WORKER_SELF_TOOLS | BOARD_SELF_TOOLS | {"terminal", "write_file"}


def _registry_with(names) -> ToolRegistry:
    reg = ToolRegistry()
    for name in names:
        reg.register(
            name, ToolSchema(name=name, description="t", parameters={}),
            lambda _a, **_k: "{}",
        )
    return reg


def _worker_session(**extra):
    session = _session_with_config({"allowed_tools": sorted(_ALLOWED), **extra})
    session.task_id = uuid4()
    return session


def test_task_worker_keeps_self_tools_under_allowlist():
    harness = _make_harness(tool_registry=_registry_with(_ALL))
    allowed = harness._tool_filter_for_session(_worker_session())
    assert WORKER_SELF_TOOLS <= allowed      # can report completion / blockage
    assert _ALLOWED <= allowed               # its own work tools survive
    assert "terminal" not in allowed         # the allowlist still binds
    assert "write_file" not in allowed


def test_group_member_keeps_board_tools_under_allowlist():
    harness = _make_harness(tool_registry=_registry_with(_ALL))
    session = _worker_session(context_group_id=str(uuid4()))
    allowed = harness._tool_filter_for_session(session)
    assert BOARD_SELF_TOOLS <= allowed


def test_solo_allowlisted_session_gets_no_self_tools():
    """No task row and no group: the allowlist is taken verbatim."""
    harness = _make_harness(tool_registry=_registry_with(_ALL))
    allowed = harness._tool_filter_for_session(
        _session_with_config({"allowed_tools": sorted(_ALLOWED)})
    )
    assert not (WORKER_SELF_TOOLS & allowed)
    assert not (BOARD_SELF_TOOLS & allowed)
    assert allowed == _ALLOWED
