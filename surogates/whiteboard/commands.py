"""Structural validation for a ``whiteboard_draw`` command list.

Pure functions, no I/O.  These rules are a port of the structural half of
PenEcho's ``normalize`` (``study/penecho/public/draw.js:123``) plus its
server-side response validator.  Deliberately structural only: geometry
(bounding boxes, curve extrema, arrowheads) is computed by the vendored
``draw.js`` in the browser, which remains authoritative for rendering.
The point of validating here is that a malformed list becomes a model
retry with a precise message instead of an object the client silently
drops.
"""
from __future__ import annotations

import math
from typing import Any

#: The canvas is infinite: it has no edges, the origin is the middle of
#: nowhere in particular, and coordinates are freely negative.  This is
#: only a sanity bound, so a malformed or hallucinated 1e300 is rejected
#: instead of being placed somewhere no viewport can ever reach.  Nothing
#: about the surface changes at this value; it is not a wall the user can
#: hit by panning.
COORD_LIMIT = 1_000_000

#: Commands per ``whiteboard_draw`` call.
MAX_COMMANDS = 16

#: ``draw`` limits, from ``draw.js:7-11``.
MAX_DRAW_ITEMS = 64
MAX_DRAW_VALUES = 2_048
DRAW_TYPES = frozenset({"line", "smooth", "rect", "ellipse", "circle", "arc"})

WHITEBOARD_TOOLS: frozenset[str] = frozenset({
    "write_text", "draw_formula", "draw", "erase", "place_artifact",
})

#: Required keys per command tool.  ``write_text`` requires ``maxWidth``
#: because the model owns layout: without an explicit wrap width the
#: client has to guess one, and the guess is wrong often enough that
#: PenEcho's prompt makes it mandatory too.
_REQUIRED: dict[str, tuple[str, ...]] = {
    "write_text": ("x", "y", "text", "fontSize", "maxWidth"),
    "draw_formula": ("x", "y", "latex", "fontSize"),
    "draw": ("origin", "types", "items"),
    "erase": ("mode",),
    "place_artifact": ("artifact_id", "x", "y", "w", "h"),
}

#: Keys holding a position.  Freely negative: the origin is arbitrary.
_POSITION_KEYS = frozenset({"x", "y"})

#: Commands that may be placed relationally -- by naming an anchor
#: instead of computing coordinates.  ``draw`` stays absolute: a sketch
#: is genuinely geometric.  ``erase`` has nothing to anchor to.
_RELATIONAL_TOOLS = frozenset({"write_text", "draw_formula", "place_artifact"})

#: The named anchors that exist without an object id: the user's newest
#: ink and their lasso.  Anything else must be an earlier call id.
_NAMED_ANCHORS = frozenset({"latest", "selection"})

#: Where an anchored command sits relative to its anchor.
_SIDES = frozenset({"right", "below", "above", "left", "in"})

#: What an anchored command still has to say itself.  Geometry and
#: sizing come from the anchor; the content cannot.
_REQUIRED_ANCHORED: dict[str, tuple[str, ...]] = {
    "write_text": ("text",),
    "draw_formula": ("latex",),
    "place_artifact": ("artifact_id", "w", "h"),
    # Into a slot only: the sketch is drawn in a 1000x1000 local box and
    # the client scales it in, so there is no origin to give.
    "draw": ("types", "items"),
}

#: The intents a call may declare.  Declared, not inferred: it makes the
#: model's reading of the situation inspectable and lets the tool push
#: back when the board disagrees.
INTENTS = frozenset({"fill", "continue", "transform", "respond"})


def _is_anchored(cmd: dict[str, Any]) -> bool:
    """Whether *cmd* delegates placement to the client.

    Either an explicit anchor, or a ``replaces`` with no coordinates --
    a revision with nowhere named to go takes the replaced object's
    place.
    """
    if isinstance(cmd.get("anchor"), str) and cmd["anchor"].strip():
        return True
    return (
        isinstance(cmd.get("replaces"), str)
        and bool(cmd["replaces"].strip())
        and "x" not in cmd
        and "y" not in cmd
    )


def _validate_relational(
    cmd: dict[str, Any], idx: int, tool: str,
) -> str | None:
    """Shape-check ``anchor``/``side``; both are optional."""
    anchor = cmd.get("anchor")
    if anchor is not None:
        if tool == "draw" and cmd.get("side") != "in":
            return (
                f"command[{idx}] (draw): a sketch is anchored only into a "
                f"slot -- anchor:'S1', side:'in', with items in a "
                f"1000x1000 local box. Elsewhere give origin."
            )
        if tool not in _RELATIONAL_TOOLS and tool != "draw":
            return (
                f"command[{idx}] ({tool}): anchor is not supported here. "
                f"Anchored placement: {', '.join(sorted(_RELATIONAL_TOOLS))}."
            )
        if not (isinstance(anchor, str) and anchor.strip()):
            return (
                f"command[{idx}] ({tool}): anchor must be 'latest', "
                f"'selection', or the id of an earlier whiteboard_draw "
                f"call."
            )
    side = cmd.get("side")
    if side is not None:
        if anchor is None and not _is_anchored(cmd):
            return (
                f"command[{idx}] ({tool}): side only makes sense with an "
                f"anchor (or a replaces that stands in for one)."
            )
        if side not in _SIDES:
            return (
                f"command[{idx}] ({tool}): unknown side {side!r}. Valid "
                f"sides: {', '.join(sorted(_SIDES))}."
            )
    return None

#: Keys holding an extent.  A negative width is meaningless however
#: infinite the canvas is, and silently renders nothing.
_SIZE_KEYS = frozenset({"w", "h", "maxWidth"})

#: ``width``/``height`` accepted where the schema says ``w``/``h``.
#:
#: The size vocabulary is mixed across commands -- ``draw`` has a stroke
#: ``width``, ``write_text`` has ``maxWidth``, these two have ``w``/``h``
#: -- so reaching for the long spelling is an easy slip, and it cost a
#: real session its erase: the model wrote a wrong correction, tried
#: twice to rub it out with ``width``/``height``, was rejected both
#: times, and left the mistake on the user's canvas.  Aliased rather
#: than rejected for the same reason the handler recovers a JSON-string
#: command array: the intent is unambiguous and a retry buys nothing.
#: The client applies the same aliases.
_SIZE_ALIASES = {"w": "width", "h": "height"}

#: Commands whose schema uses ``w``/``h``, and so honour the aliases.
#: ``draw`` is deliberately absent: its ``width`` is a stroke weight, not
#: an extent, and aliasing it would silently change what it draws.
_ALIASING_TOOLS = frozenset({"erase", "place_artifact"})


def _with_size_aliases(cmd: dict[str, Any]) -> dict[str, Any]:
    """*cmd* with ``w``/``h`` filled in from ``width``/``height``.

    Returns a copy: validation is pure, and the durable command in the
    event log stays exactly as the model wrote it.
    """
    resolved = dict(cmd)
    for key, alias in _SIZE_ALIASES.items():
        if key not in resolved and alias in resolved:
            resolved[key] = resolved[alias]
    return resolved


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _in_canvas(value: Any) -> bool:
    """Whether *value* is a plausible canvas position.

    Negative is ordinary on an infinite canvas -- the origin is arbitrary
    and content spreads in every direction.
    """
    return _is_number(value) and -COORD_LIMIT <= value <= COORD_LIMIT


def _is_extent(value: Any) -> bool:
    """Whether *value* is a plausible width or height."""
    return _is_number(value) and 0 < value <= COORD_LIMIT


def _is_int(value: Any, lo: int, hi: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and lo <= value <= hi
    )


def _validate_draw(cmd: dict[str, Any], idx: int) -> str | None:
    origin = cmd.get("origin")
    into_slot = cmd.get("side") == "in" and _is_anchored(cmd)
    if not into_slot and not (
        isinstance(origin, list)
        and len(origin) == 2
        and all(_in_canvas(v) for v in origin)
    ):
        return (
            f"command[{idx}] draw: origin must be [x, y] within "
            f"+/-{COORD_LIMIT}."
        )

    types, items = cmd.get("types"), cmd.get("items")
    if not isinstance(types, list) or not isinstance(items, list):
        return f"command[{idx}] draw: types and items must both be arrays."
    if not types:
        return f"command[{idx}] draw: types must not be empty."
    if len(types) != len(items):
        return (
            f"command[{idx}] draw: types and items must have the same "
            f"length (got {len(types)} and {len(items)})."
        )
    if len(types) > MAX_DRAW_ITEMS:
        return (
            f"command[{idx}] draw: at most {MAX_DRAW_ITEMS} items "
            f"(got {len(types)})."
        )

    total_values = 0
    for i, (kind, item) in enumerate(zip(types, items)):
        if kind not in DRAW_TYPES:
            valid = ", ".join(sorted(DRAW_TYPES))
            return (
                f"command[{idx}] draw: item {i} has unknown type "
                f"{kind!r}. Valid types: {valid}."
            )
        if not isinstance(item, list) or not item:
            return f"command[{idx}] draw: item {i} must be a non-empty array."
        # Item coordinates are offsets from origin, so they may be
        # negative; the magnitude bound is what matters.
        if not all(_is_int(v, -COORD_LIMIT, COORD_LIMIT) for v in item):
            return (
                f"command[{idx}] draw: item {i} values must be integers "
                f"within +/-{COORD_LIMIT}."
            )
        total_values += len(item)
        if total_values > MAX_DRAW_VALUES:
            return (
                f"command[{idx}] draw: at most {MAX_DRAW_VALUES} coordinate "
                f"values across all items."
            )

    if "width" in cmd and not _is_int(cmd["width"], 2, 200):
        return f"command[{idx}] draw: width must be an integer 2..200."
    if "tension" in cmd and not _is_int(cmd["tension"], 0, 100):
        return f"command[{idx}] draw: tension must be an integer 0..100."
    return None


def _validate_erase(cmd: dict[str, Any], idx: int) -> str | None:
    mode = cmd.get("mode")
    if mode == "rect":
        missing = [k for k in ("x", "y", "w", "h") if k not in cmd]
        if missing:
            return (
                f"command[{idx}] erase mode=rect requires "
                f"{', '.join(missing)}."
            )
        return None
    if mode == "path":
        points = cmd.get("points")
        if not (
            isinstance(points, list)
            and points
            and all(
                isinstance(p, list)
                and len(p) == 2
                and all(_in_canvas(v) for v in p)
                for p in points
            )
        ):
            return (
                f"command[{idx}] erase mode=path requires points as a "
                f"non-empty array of [x, y] pairs."
            )
        return None
    return (
        f"command[{idx}] erase: unknown mode {mode!r}. "
        f"Valid modes: rect, path."
    )


#: Average glyph advance as a fraction of font size, for a proportional
#: face. Only ever used to predict *whether* text wraps, never to lay it
#: out -- the client measures for real at paint time.
_GLYPH_ADVANCE = 0.6


def _wrapped_lines(cmd: dict[str, Any]) -> int:
    """How many lines a ``write_text`` is likely to occupy."""
    text = str(cmd.get("text") or "")
    font = cmd.get("fontSize")
    max_width = cmd.get("maxWidth")
    if not (_is_number(font) and _is_number(max_width)) or max_width <= 0:
        return 1
    lines = 0
    for paragraph in text.split("\n"):
        width = len(paragraph) * font * _GLYPH_ADVANCE
        lines += max(1, math.ceil(width / max_width))
    return max(1, lines)


def _text_tower(commands: list[Any]) -> str | None:
    """Reject prose wrapped into a column taller than it is wide.

    ``fontSize`` and ``maxWidth`` are chosen independently, and nothing
    connects them: told to match handwriting of 80 units, the model set
    fontSize 75 and left maxWidth at 400, which is about six characters
    a line. Its one-sentence answer became nine lines and 877 units of
    tower -- taller than the whole captured board -- straight down the
    middle of the user's working.

    Matching handwriting is right for a short answer and wrong for a
    sentence; what actually matters is that the block ends up shaped
    like text. Four lines is the floor so a genuine short paragraph is
    never touched.
    """
    for idx, cmd in enumerate(commands):
        if not isinstance(cmd, dict) or cmd.get("tool") != "write_text":
            continue
        lines = _wrapped_lines(cmd)
        if lines < 4:
            continue
        font, max_width = cmd["fontSize"], cmd["maxWidth"]
        line_height = cmd.get("lineHeight")
        step = font * (line_height if _is_number(line_height) else 1.35)
        height = lines * step
        if height <= max_width:
            continue
        fits = math.ceil(len(str(cmd.get("text") or "")) * font * _GLYPH_ADVANCE)
        return (
            f"command[{idx}] (write_text) wraps onto {lines} lines at "
            f"fontSize={font:g} and maxWidth={max_width:g}, a block "
            f"{height:g} tall and only {max_width:g} wide -- a tower, not "
            f"a paragraph. A line holds about maxWidth/(fontSize*0.6) "
            f"characters. Either set maxWidth near {fits} so it reads "
            f"across, or use a smaller fontSize: handwriting scale suits "
            f"a short answer, not a sentence."
        )
    return None


def _wrap_collision(commands: list[Any]) -> str | None:
    """Reject a call whose wrapped text would land on its own next command.

    The model picks ``maxWidth`` and ``fontSize`` but cannot measure the
    result, so it spaces the next command as if the text were one line.
    A real call left "Yes, we can factor it a bit:" wrapping onto two
    lines 90 units apart from a formula, and the second line printed
    straight through it.

    Deliberately narrow: it fires only when text is predicted to wrap
    *and* a later command sits inside the extra lines. Everything else is
    left to the model's judgement, because the estimate is too rough to
    police general overlap.
    """
    for idx, cmd in enumerate(commands):
        if not isinstance(cmd, dict) or cmd.get("tool") != "write_text":
            continue
        lines = _wrapped_lines(cmd)
        if lines < 2:
            continue
        font = cmd["fontSize"]
        line_height = cmd.get("lineHeight")
        step = font * (line_height if _is_number(line_height) else 1.35)
        top, left = cmd.get("y"), cmd.get("x")
        if not (_is_number(top) and _is_number(left)):
            continue
        bottom = top + lines * step
        right = left + cmd["maxWidth"]

        for other_idx, other in enumerate(commands):
            if other_idx == idx or not isinstance(other, dict):
                continue
            ox, oy = other.get("x"), other.get("y")
            if not (_is_number(ox) and _is_number(oy)):
                continue
            # From the second line down, and inclusive on the left edge:
            # stacked commands share an x, which is exactly the case
            # this exists to catch.
            if top + step <= oy < bottom and left <= ox < right:
                return (
                    f"command[{idx}] (write_text) wraps onto {lines} lines "
                    f"at maxWidth={cmd['maxWidth']} and would run through "
                    f"command[{other_idx}] at y={oy}. Text occupies "
                    f"fontSize*lineHeight ({step:g}) per line: leave "
                    f"{lines * step:g} below y={top}, or raise maxWidth so "
                    f"it fits on one line."
                )
    return None


def validate_commands(commands: Any) -> str | None:
    """Return an error message, or ``None`` when *commands* is valid."""
    if not isinstance(commands, list):
        return "commands must be an array."
    if not commands:
        return "commands must contain at least one command."
    if len(commands) > MAX_COMMANDS:
        return (
            f"At most {MAX_COMMANDS} commands per call (got "
            f"{len(commands)}). Split the work across turns."
        )

    for idx, cmd in enumerate(commands):
        if not isinstance(cmd, dict):
            return f"command[{idx}] must be an object."
        tool = cmd.get("tool")
        if not tool:
            return f"command[{idx}] is missing the required 'tool' key."
        if tool not in WHITEBOARD_TOOLS:
            valid = ", ".join(sorted(WHITEBOARD_TOOLS))
            return (
                f"command[{idx}] has unknown tool {tool!r}. "
                f"Valid tools: {valid}."
            )

        # Optional on any drawing command: the id of an earlier
        # ``whiteboard_draw`` call whose objects this one supersedes.
        # Without it the model can only add, so revising an answer means
        # drawing over the old one -- ``erase`` paints white, it does not
        # delete. One turn stacked four answers on a single spot.
        replaces = cmd.get("replaces")
        if replaces is not None and not (
            isinstance(replaces, str) and replaces.strip()
        ):
            return (
                f"command[{idx}] ({tool}): replaces must be the id of an "
                f"earlier whiteboard_draw call."
            )

        if tool in _ALIASING_TOOLS:
            cmd = _with_size_aliases(cmd)

        err = _validate_relational(cmd, idx, tool)
        if err:
            return err

        # An anchored command delegates geometry to the client: position
        # comes from the anchor, and for text/formula the sizes default
        # from the anchor's height.  Requiring x/y/fontSize here would
        # force the model back into the coordinate arithmetic that
        # relational placement exists to end.
        anchored = _is_anchored(cmd) and tool in _REQUIRED_ANCHORED
        required = _REQUIRED_ANCHORED[tool] if anchored else _REQUIRED[tool]
        missing = [k for k in required if k not in cmd]
        if missing:
            return (
                f"command[{idx}] ({tool}) is missing required "
                f"field(s): {', '.join(missing)}."
            )

        for key in _POSITION_KEYS & cmd.keys():
            if not _in_canvas(cmd[key]):
                return (
                    f"command[{idx}] ({tool}): {key}={cmd[key]!r} is "
                    f"outside the +/-{COORD_LIMIT} coordinate range."
                )
        for key in _SIZE_KEYS & cmd.keys():
            if not _is_extent(cmd[key]):
                return (
                    f"command[{idx}] ({tool}): {key}={cmd[key]!r} must be "
                    f"a positive size no larger than {COORD_LIMIT}."
                )

        if tool == "draw":
            err = _validate_draw(cmd, idx)
            if err:
                return err
        elif tool == "erase":
            err = _validate_erase(cmd, idx)
            if err:
                return err
        elif tool == "place_artifact":
            if not isinstance(cmd["artifact_id"], str) or not cmd["artifact_id"]:
                return (
                    f"command[{idx}] place_artifact: artifact_id must be a "
                    f"non-empty string."
                )

    return _text_tower(commands) or _wrap_collision(commands)


#: Readings per call, and the longest one.  A transcription is a line of
#: handwriting, not an essay.
MAX_READINGS = 32
MAX_READING_CHARS = 400


def validate_readings(readings: Any) -> str | None:
    """Shape-check the ``readings`` array of a ``whiteboard_draw`` call.

    Each entry transcribes one labelled ink mark: ``{mark, text}``.  The
    client stores it against the mark's strokes, so the next turn hands
    the model text instead of pixels for everything it has already read.
    Optional: ``None`` when absent.
    """
    if readings is None:
        return None
    if not isinstance(readings, list):
        return "readings must be an array of {mark, text}."
    if len(readings) > MAX_READINGS:
        return f"At most {MAX_READINGS} readings per call."
    for idx, entry in enumerate(readings):
        if not isinstance(entry, dict):
            return f"readings[{idx}] must be an object with mark and text."
        mark = entry.get("mark")
        text = entry.get("text")
        if not (isinstance(mark, str) and mark.strip()):
            return f"readings[{idx}]: mark must be a label such as 'A2'."
        if not isinstance(text, str):
            return f"readings[{idx}]: text must be a string."
        if len(text) > MAX_READING_CHARS:
            return (
                f"readings[{idx}]: text is {len(text)} characters; a "
                f"reading is one line of handwriting, at most "
                f"{MAX_READING_CHARS}."
            )
    return None


def validate_intent(intent: Any) -> str | None:
    """Shape-check the optional top-level ``intent`` of a draw call."""
    if intent is None:
        return None
    if not (isinstance(intent, str) and intent in INTENTS):
        return (
            f"intent must be one of {', '.join(sorted(INTENTS))} "
            f"(got {intent!r})."
        )
    return None


def unfilled_slots(commands: Any, slots: frozenset[str]) -> list[str]:
    """Slots the user reserved that *commands* leave empty.

    A slot is the user's own answer to "where does the result go", and
    an unfilled one means the model answered somewhere else -- or in
    prose.  Returned in label order so the message names the first.
    """
    if not slots or not isinstance(commands, list):
        return sorted(slots) if slots else []
    filled = {
        cmd.get("anchor")
        for cmd in commands
        if isinstance(cmd, dict) and cmd.get("side") == "in"
        and isinstance(cmd.get("anchor"), str)
    }
    return sorted(slot for slot in slots if slot not in filled)
