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


# ---------------------------------------------------------------------
# width/height accepted where the schema says w/h
#
# From a real session: the model wrote a wrong correction on the user's
# board, tried twice to rub it out, and was rejected both times for
# spelling the extents ``width``/``height``.  The mistake stayed on the
# canvas and the user paused the session.
# ---------------------------------------------------------------------


def test_erase_rect_accepts_width_and_height():
    # Verbatim the call that failed.
    assert validate_commands([
        {"tool": "erase", "mode": "rect",
         "x": 460, "y": 300, "width": 460, "height": 60},
    ]) is None


def test_erase_rect_still_rejects_no_extents_at_all():
    error = validate_commands([
        {"tool": "erase", "mode": "rect", "x": 460, "y": 300},
    ])
    assert error is not None
    assert "w, h" in error


def test_erase_rect_validates_an_aliased_extent():
    # The alias is a spelling, not an escape from the range checks.
    error = validate_commands([
        {"tool": "erase", "mode": "rect",
         "x": 0, "y": 0, "width": -5, "height": 60},
    ])
    assert error is not None


def test_place_artifact_accepts_width_and_height():
    assert validate_commands([
        {"tool": "place_artifact", "artifact_id": "a1",
         "x": 0, "y": 0, "width": 100, "height": 50},
    ]) is None


def test_the_short_spelling_still_wins():
    # Both present: w/h is the schema, so it is what counts.
    assert validate_commands([
        {"tool": "erase", "mode": "rect", "x": 0, "y": 0,
         "w": 10, "h": 10, "width": -999, "height": -999},
    ]) is None


def test_draw_width_is_a_stroke_weight_not_an_extent():
    # ``draw`` must NOT alias: its width is a stroke weight bounded
    # 2..200, and treating it as an extent would change what it draws.
    error = validate_commands([
        {"tool": "draw", "origin": [0, 0], "types": ["line"],
         "items": [[0, 0, 10, 10]], "width": 500},
    ])
    assert error is not None
    assert "stroke" in error or "2..200" in error


# ---------------------------------------------------------------------
# replaces: superseding an earlier draw
#
# The agent can only add objects, so revising an answer meant drawing
# over the old one -- erase paints white, it does not delete. One turn
# stacked four answers on a single spot.
# ---------------------------------------------------------------------


def test_accepts_a_replaces_id():
    assert validate_commands([
        {"tool": "write_text", "x": 0, "y": 0, "text": "3", "fontSize": 40,
         "maxWidth": 100, "replaces": "toolu_01A"},
    ]) is None


def test_rejects_a_non_string_replaces():
    error = validate_commands([
        {"tool": "write_text", "x": 0, "y": 0, "text": "3", "fontSize": 40,
         "maxWidth": 100, "replaces": 17},
    ])
    assert error is not None and "replaces" in error


def test_rejects_an_empty_replaces():
    error = validate_commands([
        {"tool": "write_text", "x": 0, "y": 0, "text": "3", "fontSize": 40,
         "maxWidth": 100, "replaces": "   "},
    ])
    assert error is not None and "replaces" in error


def test_replaces_is_optional():
    assert validate_commands([
        {"tool": "write_text", "x": 0, "y": 0, "text": "3", "fontSize": 40,
         "maxWidth": 100},
    ]) is None


# ---------------------------------------------------------------------
# Wrapped text running through the next command
#
# The model picks maxWidth and fontSize but cannot measure the result,
# so it spaces the next command as if the text were one line. A real
# call left "Yes, we can factor it a bit:" wrapping onto two lines 90
# units apart from a formula, and the second line printed through it.
# ---------------------------------------------------------------------


def _wrapping_pair(second_y):
    return [
        {"x": 1520, "y": 610, "text": "Yes, we can factor it a bit:",
         "tool": "write_text", "fontSize": 65, "maxWidth": 700,
         "lineHeight": 1.3},
        {"x": 1520, "y": second_y, "tool": "draw_formula",
         "latex": "e^{2x} + 2e^x(x + C)", "fontSize": 65},
    ]


def test_rejects_wrapped_text_running_through_the_next_command():
    error = validate_commands(_wrapping_pair(700))
    assert error is not None
    assert "wraps onto 2 lines" in error
    assert "y=700" in error


def test_accepts_the_same_pair_spaced_for_the_wrap():
    assert validate_commands(_wrapping_pair(790)) is None


def test_accepts_it_when_maxWidth_avoids_the_wrap():
    commands = _wrapping_pair(700)
    commands[0]["maxWidth"] = 1200
    assert validate_commands(commands) is None


def test_short_text_never_trips_the_check():
    assert validate_commands([
        {"x": 0, "y": 0, "text": "5", "tool": "write_text",
         "fontSize": 40, "maxWidth": 400},
        {"x": 0, "y": 60, "tool": "draw_formula", "latex": "x", "fontSize": 40},
    ]) is None


def test_a_command_beside_the_text_is_not_a_collision():
    # Wrapping only runs downward; something to the right is fine.
    commands = _wrapping_pair(700)
    commands[1]["x"] = 3000
    assert validate_commands(commands) is None


def test_the_check_survives_a_command_without_coordinates():
    commands = _wrapping_pair(700)
    commands[1] = {"tool": "erase", "mode": "path", "points": [[0, 0], [1, 1]]}
    assert validate_commands(commands) is None


# ---------------------------------------------------------------------
# Prose wrapped into a tower
#
# fontSize and maxWidth are chosen independently. Told to match 80-unit
# handwriting the model set fontSize 75 and left maxWidth at 400 -- six
# characters a line -- turning a one-sentence answer into nine lines and
# 877 units of tower, taller than the whole captured board.
# ---------------------------------------------------------------------

_SENTENCE = (
    "Yes! The integral of e^x is e^x + C because the derivative of "
    "e^x is e^x."
)


def _prose(**over):
    return [{"x": 900, "y": 200, "tool": "write_text", "text": _SENTENCE,
             "fontSize": 75, "maxWidth": 400, "lineHeight": 1.3, **over}]


def test_rejects_a_tower_of_text():
    error = validate_commands(_prose())
    assert error is not None
    assert "tower, not a paragraph" in error
    assert "9 lines" in error


def test_accepts_the_same_sentence_read_across():
    assert validate_commands(_prose(maxWidth=3300)) is None


def test_accepts_the_same_sentence_at_a_readable_size():
    assert validate_commands(_prose(fontSize=28, maxWidth=700)) is None


def test_a_short_answer_at_handwriting_scale_is_fine():
    # The whole point of the inkHeight signal: a number or a formula
    # should match the user's hand.
    assert validate_commands(_prose(text="e^x + C")) is None


def test_a_genuine_short_paragraph_is_untouched():
    # Three lines never trips it, and a wide block never does either.
    assert validate_commands(_prose(fontSize=30, maxWidth=900)) is None
