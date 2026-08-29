"""The whiteboard_draw tool: schema, routing, and handler behaviour."""
import asyncio
import json

from surogates.tools.builtin.whiteboard import (
    WHITEBOARD_TOOL_NAMES,
    register,
)
from surogates.tools.registry import ToolRegistry


def _registry():
    registry = ToolRegistry()
    register(registry)
    return registry


def _call(registry, arguments, **kwargs):
    entry = registry.get("whiteboard_draw")
    return asyncio.run(entry.handler(arguments, **kwargs))


def test_registers_under_the_whiteboard_toolset():
    entry = _registry().get("whiteboard_draw")
    assert entry.toolset == "whiteboard"


def test_schema_declares_commands_as_required():
    schema = _registry().get("whiteboard_draw").schema
    assert schema.parameters["required"] == ["commands"]
    assert schema.parameters["properties"]["commands"]["type"] == "array"


def test_routes_to_harness():
    """Regression: a tool absent from TOOL_LOCATIONS falls back to SANDBOX
    routing and dies there as 'Unknown tool'.
    """
    from surogates.tools.router import TOOL_LOCATIONS, ToolLocation

    for name in WHITEBOARD_TOOL_NAMES:
        assert TOOL_LOCATIONS.get(name) is ToolLocation.HARNESS, (
            f"{name} is not HARNESS-routed; the sandbox fallback surfaces "
            f"it as 'Unknown tool'"
        )


def test_handler_accepts_a_valid_command_list():
    result = _call(_registry(), {"commands": [{
        "tool": "write_text", "x": 10, "y": 20, "text": "5",
        "fontSize": 32, "maxWidth": 300,
    }]})
    assert "1" in result and "error" not in result.lower()


def test_handler_reports_the_object_count():
    result = _call(_registry(), {"commands": [
        {"tool": "write_text", "x": 1, "y": 1, "text": "a",
         "fontSize": 20, "maxWidth": 100},
        {"tool": "erase", "mode": "rect", "x": 0, "y": 0, "w": 5, "h": 5},
    ]})
    assert "2" in result


def test_handler_rejects_an_invalid_command_with_a_precise_message():
    result = _call(_registry(), {"commands": [{"tool": "nope"}]})
    assert "nope" in result


def test_handler_rejects_a_missing_commands_key():
    result = _call(_registry(), {})
    assert "commands" in result


def test_handler_recovers_a_json_encoded_commands_string():
    """Some models serialise the array as a string; recover rather than
    burn a retry on a silent-tic problem."""
    encoded = json.dumps([{
        "tool": "write_text", "x": 1, "y": 1, "text": "a",
        "fontSize": 20, "maxWidth": 100,
    }])
    result = _call(_registry(), {"commands": encoded})
    assert "1" in result and "error" not in result.lower()


def test_handler_rejects_a_malformed_commands_string():
    result = _call(_registry(), {"commands": "[{"})
    assert "commands" in result.lower()


def test_description_names_every_command_tool():
    description = _registry().get("whiteboard_draw").schema.description
    for tool in ("write_text", "draw_formula", "draw", "erase",
                 "place_artifact"):
        assert tool in description


# --- what the model learns back ---------------------------------------
#
# The image and the occupied-cell list are both captured at Ask time, so
# a second iteration has no evidence its first draw happened. One real
# sketch turn drew five objects, four at the same coordinates and two
# byte-identical, because every acknowledgement read the same.


def test_result_reports_where_the_object_landed():
    out = _call(_registry(), {"commands": [
        {"tool": "draw_formula", "x": 1310, "y": 338,
         "latex": "= \\infty", "fontSize": 80},
    ]})
    assert "(1310, 338)" in out
    assert "= \\infty" in out


def test_result_warns_that_the_image_predates_the_call():
    out = _call(_registry(), {"commands": [
        {"tool": "write_text", "x": 0, "y": 0, "text": "hi",
         "fontSize": 20, "maxWidth": 100},
    ]})
    assert "captured before this call" in out
    assert "do not draw it again" in out


def test_result_lists_every_command():
    out = _call(_registry(), {"commands": [
        {"tool": "write_text", "x": 10, "y": 20, "text": "a",
         "fontSize": 20, "maxWidth": 100},
        {"tool": "write_text", "x": 30, "y": 40, "text": "b",
         "fontSize": 20, "maxWidth": 100},
    ]})
    assert "(10, 20)" in out and "(30, 40)" in out


def test_result_truncates_a_long_label():
    out = _call(_registry(), {"commands": [
        {"tool": "write_text", "x": 0, "y": 0, "text": "z" * 200,
         "fontSize": 20, "maxWidth": 100},
    ]})
    # The commands are already in the event log; echoing them whole
    # would double their cost in the next turn's replay.
    assert len(out) < 600


def test_result_survives_a_command_without_coordinates():
    # erase mode=path carries points, not x/y.
    out = _call(_registry(), {"commands": [
        {"tool": "erase", "mode": "path", "points": [[0, 0], [1, 1]]},
    ]})
    assert "Drew 1 object" in out


def test_result_acknowledges_recorded_readings():
    out = _call(_registry(), {
        "commands": [{"tool": "draw_formula", "latex": "3", "anchor": "latest"}],
        "readings": [{"mark": "A2", "text": "2x + 1 = 7"}],
    })
    assert "Recorded your reading of A2" in out


def test_result_rejects_malformed_readings():
    out = _call(_registry(), {
        "commands": [{"tool": "draw_formula", "latex": "3", "anchor": "latest"}],
        "readings": [{"text": "no mark"}],
    })
    assert out.startswith("Error:")


def test_schema_declares_readings():
    schema = _registry().get("whiteboard_draw").schema
    props = schema.parameters["properties"]
    assert "readings" in props
    assert props["readings"]["items"]["required"] == ["mark", "text"]


# --- slots --------------------------------------------------------------

def test_rejects_a_call_that_leaves_the_users_slot_empty():
    """Session 1231fab2: `H ? USE`, the model knew the answer was O and
    replied in prose beside the board. A slot is the user's own answer
    to "where does it go"; a call that ignores it is rejected before the
    client folds anything, so the retry cannot land beside a miss."""
    from surogates.whiteboard.turn import current_slots

    token = current_slots.set(frozenset({"S1"}))
    try:
        out = _call(_registry(), {"commands": [
            {"tool": "write_text", "text": "Did you mean HOUSE?",
             "anchor": "latest", "side": "below"},
        ]})
    finally:
        current_slots.reset(token)
    assert out.startswith("Error:")
    assert "S1" in out and "side:'in'" in out


def test_accepts_a_call_that_fills_the_slot():
    from surogates.whiteboard.turn import current_slots

    token = current_slots.set(frozenset({"S1"}))
    try:
        out = _call(_registry(), {"commands": [
            {"tool": "write_text", "text": "O", "anchor": "S1", "side": "in"},
        ], "intent": "fill"})
    finally:
        current_slots.reset(token)
    assert out.startswith("Drew 1 object")
    assert '"O" filling S1' in out


def test_rejects_an_unknown_intent():
    out = _call(_registry(), {"commands": [
        {"tool": "write_text", "text": "O", "anchor": "latest"},
    ], "intent": "ponder"})
    assert out.startswith("Error:")


def test_schema_declares_intent():
    props = _registry().get("whiteboard_draw").schema.parameters["properties"]
    assert props["intent"]["enum"] == ["fill", "continue", "transform", "respond"]


def test_slots_come_from_the_turn_metadata():
    from surogates.whiteboard.turn import slots_from_metadata

    meta = {"whiteboard": {"marks": [
        {"id": "A1", "kind": "ink"}, {"id": "S1", "kind": "slot"},
        {"id": "S2", "kind": "slot"}, "junk",
    ]}}
    assert slots_from_metadata(meta) == frozenset({"S1", "S2"})
    assert slots_from_metadata(None) == frozenset()
