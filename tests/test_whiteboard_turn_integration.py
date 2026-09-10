"""Whiteboard commands execute through the tool runtime."""

from surogates.tools.registry import ToolRegistry
from surogates.tools.runtime import ToolRuntime

BOARD = {"surface": "whiteboard"}
PLAIN: dict = {}

CATALOGUE = {"whiteboard_draw", "create_artifact", "web_search", "memory"}


def test_a_command_list_in_negative_space_is_accepted():
    """The origin is arbitrary: a board drawn up and to the left of it is
    ordinary, not an error."""
    import asyncio

    runtime = ToolRuntime(ToolRegistry())
    runtime.register_builtins()
    result = asyncio.run(runtime.dispatch("whiteboard_draw", {
        "commands": [
            {"tool": "write_text", "x": -4200, "y": -9100, "text": "5",
             "fontSize": 32, "maxWidth": 300},
            {"tool": "draw", "origin": [-4200, -9000],
             "types": ["circle"], "items": [[0, 0, 40]]},
        ],
    }))
    assert not result.startswith("Error:")


def test_a_valid_command_list_is_accepted_by_the_registered_tool():
    import asyncio

    runtime = ToolRuntime(ToolRegistry())
    runtime.register_builtins()
    result = asyncio.run(runtime.dispatch("whiteboard_draw", {
        "commands": [
            {"tool": "write_text", "x": 1500, "y": 2400, "text": "5",
             "fontSize": 32, "maxWidth": 300},
            {"tool": "draw", "origin": [1500, 2500],
             "types": ["circle"], "items": [[0, 0, 40]]},
        ],
    }))
    assert not result.startswith("Error:")
    assert "2" in result


def test_an_invalid_command_list_comes_back_as_a_precise_error():
    import asyncio

    runtime = ToolRuntime(ToolRegistry())
    runtime.register_builtins()
    result = asyncio.run(runtime.dispatch("whiteboard_draw", {
        "commands": [{"tool": "draw", "origin": [0, 0],
                      "types": ["line", "rect"], "items": [[0, 0, 1, 1]]}],
    }))
    assert "same length" in result
