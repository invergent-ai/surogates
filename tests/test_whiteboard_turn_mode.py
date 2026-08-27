"""sketch narrows a whiteboard turn to one round-trip; deep restores the
full catalogue."""
from types import SimpleNamespace

from surogates.harness.loop import _whiteboard_sketch_filter


def _meta(mode):
    return {"whiteboard": {"mode": mode}}


ALL = {"whiteboard_draw", "web_search", "terminal", "create_artifact"}


def _wb(tool_filter, metadata, has_whiteboard=True):
    """The filter as the loop calls it, for a board-enabled agent."""
    return _whiteboard_sketch_filter(
        tool_filter, metadata, has_whiteboard=has_whiteboard,
    )


def test_sketch_narrows_to_the_draw_tool():
    assert _wb(ALL, _meta("sketch")) == {
        "whiteboard_draw",
    }


def test_deep_leaves_the_filter_untouched():
    assert _wb(ALL, _meta("deep")) == ALL


def test_a_canvas_turn_with_no_mode_defaults_to_sketch():
    assert _wb(ALL, {"whiteboard": {}}) == {
        "whiteboard_draw",
    }


def test_an_unknown_mode_falls_back_to_sketch():
    # ``mode`` is client-supplied: an unrecognised string must never
    # promote a turn to the full catalogue.
    assert _wb(ALL, _meta("unlimited")) == {
        "whiteboard_draw",
    }


def test_an_ordinary_message_turn_is_untouched():
    # The board is a view mode, so a board-enabled agent takes plain chat
    # turns too. Keying on ``turn_mode`` alone would answer "sketch" for
    # a turn carrying no canvas block at all and narrow every one of
    # them to a single drawing tool.
    assert _wb(ALL, None) == ALL
    assert _wb(ALL, {}) == ALL
    assert _wb(ALL, {"view_context": {}}) == ALL


def test_a_malformed_canvas_block_is_untouched():
    # Client-supplied: a broken block must fall out of the board path
    # entirely rather than narrowing the turn.
    assert _wb(ALL, {"whiteboard": "nonsense"}) == ALL


def test_an_agent_without_the_board_is_never_narrowed():
    # Narrowing to a tool the agent lacks empties the schema list, and
    # ``drop_unusable_tools`` refuses to return nothing -- so the drop it
    # made would be undone and whiteboard_draw handed out anyway.
    assert _wb(ALL, _meta("sketch"), has_whiteboard=False) == ALL


def test_a_none_filter_on_sketch_materialises_to_the_draw_tool():
    # ``None`` is the "no filter applied" contract. On a sketch turn it
    # must still narrow, or sketch mode silently ships every tool.
    assert _wb(None, _meta("sketch")) == {
        "whiteboard_draw",
    }


def test_a_none_filter_on_deep_stays_none():
    assert _wb(None, _meta("deep")) is None


def test_sketch_keeps_the_draw_tool_even_if_the_filter_omitted_it():
    # The prompt-surface filter force-adds whiteboard_draw on a
    # whiteboard session; the schema surface must not then remove it.
    assert _wb({"web_search"}, _meta("sketch")) == {"whiteboard_draw"}


def test_the_returned_set_is_a_copy():
    # Callers mutate the filter downstream (entitlement exclusions, MCP
    # narrowing); handing back the module-level constant would let one
    # turn's subtraction corrupt every later turn.
    from surogates.tools.builtin.whiteboard import WHITEBOARD_TOOL_NAMES

    result = _wb(ALL, _meta("sketch"))
    result.discard("whiteboard_draw")
    assert "whiteboard_draw" in WHITEBOARD_TOOL_NAMES


# ---------------------------------------------------------------------
# Reading the turn's mode off the event log.
# ---------------------------------------------------------------------

from surogates.harness.loop import _latest_whiteboard_metadata
from surogates.session.events import EventType


def _event(type_value, data):
    return SimpleNamespace(type=type_value, data=data)


def _user_msg(metadata):
    return _event(EventType.USER_MESSAGE.value, {"metadata": metadata})


def test_reads_the_newest_user_message_metadata():
    events = [
        _user_msg(_meta("sketch")),
        _event("llm.response", {}),
        _user_msg(_meta("deep")),
    ]
    assert _latest_whiteboard_metadata(events) == _meta("deep")


def test_skips_non_user_events():
    events = [_user_msg(_meta("deep")), _event("tool.call", {"name": "x"})]
    assert _latest_whiteboard_metadata(events) == _meta("deep")


def test_returns_none_with_no_user_message():
    assert _latest_whiteboard_metadata([_event("llm.response", {})]) is None


def test_returns_none_for_an_empty_log():
    assert _latest_whiteboard_metadata([]) is None
    assert _latest_whiteboard_metadata(None) is None


def test_returns_none_when_the_newest_message_has_no_metadata():
    # An older turn's mode must not leak into a turn that declared none.
    events = [_user_msg(_meta("deep")), _user_msg(None)]
    assert _latest_whiteboard_metadata(events) is None


def test_accepts_an_enum_type_value():
    events = [_event(EventType.USER_MESSAGE, {"metadata": _meta("deep")})]
    assert _latest_whiteboard_metadata(events) == _meta("deep")


def test_tolerates_a_non_dict_event_payload():
    assert _latest_whiteboard_metadata(
        [_event(EventType.USER_MESSAGE.value, None)],
    ) is None
