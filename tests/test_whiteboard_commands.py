"""Whiteboard command-list validation.

Mirrors the structural rules PenEcho enforces in
``study/penecho/public/draw.js:123`` (``normalize``) and its server-side
response validator.  This is a cheap guard so a malformed command list
becomes a model retry instead of a silently-dropped object; the SDK's
vendored ``draw.js`` remains authoritative for rendering geometry.
"""
import pytest

from surogates.whiteboard.commands import (
    COORD_LIMIT,
    MAX_COMMANDS,
    validate_commands,
)


def _text(**over):
    base = {
        "tool": "write_text", "x": 100, "y": 200, "text": "hi",
        "fontSize": 32, "maxWidth": 400, "lineHeight": 1.35,
    }
    base.update(over)
    return base


def test_accepts_a_minimal_valid_list():
    assert validate_commands([_text()]) is None


def test_rejects_an_empty_list():
    assert "at least one command" in (validate_commands([]) or "")


def test_rejects_more_than_max_commands():
    err = validate_commands([_text()] * (MAX_COMMANDS + 1))
    assert str(MAX_COMMANDS) in (err or "")


def test_rejects_an_unknown_tool():
    err = validate_commands([{"tool": "summon_dragon"}])
    assert "summon_dragon" in (err or "")


def test_rejects_a_missing_tool_key():
    assert "tool" in (validate_commands([{"x": 1, "y": 2}]) or "")


def test_rejects_coordinates_beyond_the_sanity_bound():
    err = validate_commands([_text(x=COORD_LIMIT + 1)])
    assert "coordinate range" in (err or "")


def test_accepts_negative_coordinates():
    # The canvas is infinite and the origin is arbitrary, so content
    # spreads in every direction.
    assert validate_commands([_text(x=-5000, y=-9000)]) is None


def test_rejects_a_negative_size():
    # Infinite in extent, but a negative width still renders nothing.
    assert "positive size" in (validate_commands([_text(maxWidth=-1)]) or "")


def test_rejects_a_zero_size():
    assert "positive size" in (validate_commands([_text(maxWidth=0)]) or "")


def test_accepts_a_draw_at_negative_origin():
    assert validate_commands([{
        "tool": "draw", "origin": [-4000, -300],
        "types": ["rect"], "items": [[0, 0, 50, 50]],
    }]) is None


def test_rejects_write_text_without_maxwidth():
    bad = _text()
    del bad["maxWidth"]
    assert "maxWidth" in (validate_commands([bad]) or "")


def test_draw_requires_equal_length_types_and_items():
    err = validate_commands([{
        "tool": "draw", "origin": [0, 0],
        "types": ["line", "rect"], "items": [[0, 0, 10, 10]],
    }])
    assert "same length" in (err or "")


def test_draw_rejects_an_unknown_primitive_type():
    err = validate_commands([{
        "tool": "draw", "origin": [0, 0],
        "types": ["squiggle"], "items": [[0, 0, 1, 1]],
    }])
    assert "squiggle" in (err or "")


def test_draw_rejects_too_many_items():
    err = validate_commands([{
        "tool": "draw", "origin": [0, 0],
        "types": ["rect"] * 65, "items": [[0, 0, 1, 1]] * 65,
    }])
    assert "64" in (err or "")


def test_erase_accepts_negative_path_points():
    assert validate_commands([{
        "tool": "erase", "mode": "path",
        "points": [[-10, -10], [-5, -5]], "size": 20,
    }]) is None


def test_draw_rejects_too_many_total_values():
    # 40 items x 60 values = 2400 > MAX_VALUES (2048)
    err = validate_commands([{
        "tool": "draw", "origin": [0, 0],
        "types": ["line"] * 40, "items": [[0] * 60] * 40,
    }])
    assert "2048" in (err or "")


def test_draw_rejects_width_out_of_range():
    err = validate_commands([{
        "tool": "draw", "origin": [0, 0], "types": ["rect"],
        "items": [[0, 0, 1, 1]], "width": 500,
    }])
    assert "width" in (err or "")


def test_erase_accepts_rect_and_path_modes():
    assert validate_commands([
        {"tool": "erase", "mode": "rect", "x": 0, "y": 0, "w": 10, "h": 10},
    ]) is None
    assert validate_commands([
        {"tool": "erase", "mode": "path", "points": [[0, 0], [5, 5]], "size": 20},
    ]) is None


def test_erase_rejects_an_unknown_mode():
    err = validate_commands([{"tool": "erase", "mode": "vanish"}])
    assert "vanish" in (err or "")


def test_place_artifact_requires_an_artifact_id():
    err = validate_commands([
        {"tool": "place_artifact", "x": 0, "y": 0, "w": 100, "h": 100},
    ])
    assert "artifact_id" in (err or "")


def test_draw_formula_requires_latex():
    err = validate_commands([
        {"tool": "draw_formula", "x": 0, "y": 0, "fontSize": 40},
    ])
    assert "latex" in (err or "")


@pytest.mark.parametrize("bad", ["not a list", None, 42, {"tool": "write_text"}])
def test_rejects_a_non_list_payload(bad):
    assert validate_commands(bad) is not None
