"""Board tools visible iff the session carries context_group_id."""
from types import SimpleNamespace

from surogates.orchestrator.worker import _filter_effective_tools


def _tenant():
    return SimpleNamespace(org_id="o", user_id="u")


def _session(config=None, channel="web"):
    return SimpleNamespace(
        config=config or {}, channel=channel,
        service_account_id=None, task_id=None,
    )


def test_board_tools_stripped_without_group():
    result = _filter_effective_tools(
        tools={"share_note", "read_board", "expand_note", "memory"},
        tenant=_tenant(),
        session=_session(),
        use_api_for_harness_tools=True,
    )
    assert not ({"share_note", "read_board", "expand_note"} & result)
    assert "memory" in result


def test_board_tools_force_added_with_group():
    # Even when an AgentDef allowlist omitted them, group members get
    # their coordination self-tools (worker_* idiom).
    result = _filter_effective_tools(
        tools={"memory"},
        tenant=_tenant(),
        session=_session(config={"context_group_id": "g-1"}),
        use_api_for_harness_tools=True,
    )
    assert {"share_note", "read_board", "expand_note"} <= result


def test_board_tools_route_to_harness():
    """Regression: tools absent from TOOL_LOCATIONS fall back to SANDBOX
    routing and die there as 'Unknown tool' (no DB in the sandbox pod) —
    exactly how share_note/read_board failed in production. Every board
    tool must be explicitly HARNESS-routed, like the task layer.
    """
    from surogates.board.tools import BOARD_TOOLS
    from surogates.tools.router import TOOL_LOCATIONS, ToolLocation

    for name in BOARD_TOOLS:
        assert TOOL_LOCATIONS.get(name) is ToolLocation.HARNESS, (
            f"{name} is not HARNESS-routed; sandbox fallback surfaces "
            f"it as 'Unknown tool'"
        )
