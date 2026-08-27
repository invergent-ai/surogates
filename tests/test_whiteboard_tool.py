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
