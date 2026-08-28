"""Builtin ``whiteboard_draw`` tool.

The agent's write path onto the canvas.  The tool call itself carries the
payload: the SDK renders straight off the ``tool.call`` event's arguments,
so drawing begins as the call streams rather than after the result lands.
The handler therefore validates and acknowledges; it does not persist.
The client is the sole writer of the canvas document (see the design doc's
"Persistence: single writer").
"""
from __future__ import annotations

import json
import logging
from typing import Any

from surogates.tools.registry import ToolRegistry, ToolSchema
from surogates.whiteboard.commands import (
    COORD_LIMIT,
    MAX_COMMANDS,
    validate_commands,
)

logger = logging.getLogger(__name__)

WHITEBOARD_TOOL_NAMES: frozenset[str] = frozenset({"whiteboard_draw"})

_DESCRIPTION = (
    "Draw on the shared whiteboard canvas. The canvas is infinite: there "
    "are no edges, the origin is arbitrary, and coordinates are freely "
    "negative. Every coordinate is a global canvas coordinate -- never an "
    "image coordinate. Use the geometry in the turn's canvas note to "
    "convert.\n\n"
    "Commands:\n"
    "- write_text {tool,x,y,text,fontSize,maxWidth,lineHeight?} -- prose. "
    "You own layout: x,y is the top-left start and maxWidth is the wrap "
    "width. Pick a blank region near the content you are answering.\n"
    "- draw_formula {tool,x,y,latex,fontSize} -- mathematical notation.\n"
    "- draw {tool,origin:[x,y],types:[...],items:[[...]],width?,tension?,"
    "closed?,fill?,arrows?} -- a simple sketch or annotation of about ten "
    "or fewer primitives. types and items must be the same length. "
    "Encodings: line/smooth [x1,y1,x2,y2,...]; rect [x,y,w,h]; ellipse "
    "[cx,cy,rx,ry]; circle [cx,cy,r]; arc [cx,cy,rx,ry,startDeg,sweepDeg]. "
    "Item coordinates are integer offsets from origin.\n"
    "- erase {tool,mode:'rect',x,y,w,h} or {tool,mode:'path',points,size}\n"
    "- place_artifact {tool,artifact_id,x,y,w,h} -- position an artifact "
    "you already created with create_artifact. For anything larger, "
    "richer, or interactive than a simple sketch -- a chart, a diagram, a "
    "table, an interactive widget -- create an artifact and place it "
    "rather than approximating it with many draw commands.\n\n"
    "Any command may carry `replaces`: the id of an earlier "
    "whiteboard_draw call whose objects it supersedes. Use it to correct "
    "or update something you drew before -- the old objects are removed "
    "as the new one lands, so a revised answer replaces the previous one "
    "instead of piling on top of it. The turn note lists what you drew "
    "and where it now sits.\n\n"
    f"At most {MAX_COMMANDS} commands per call. Do not redraw content "
    "that is already on the canvas: add only the continuation, answer, or "
    "annotation that is missing."
)


def register(registry: ToolRegistry) -> None:
    """Register the ``whiteboard_draw`` tool."""
    registry.register(
        name="whiteboard_draw",
        schema=ToolSchema(
            name="whiteboard_draw",
            description=_DESCRIPTION,
            parameters={
                "type": "object",
                "properties": {
                    "commands": {
                        "type": "array",
                        "maxItems": MAX_COMMANDS,
                        "description": (
                            "Ordered list of drawing commands. Each object "
                            "must carry a 'tool' key naming one of "
                            "write_text, draw_formula, draw, erase, "
                            "place_artifact."
                        ),
                        "items": {
                            "type": "object",
                            "required": ["tool"],
                            "properties": {
                                "tool": {
                                    "type": "string",
                                    "enum": [
                                        "write_text", "draw_formula", "draw",
                                        "erase", "place_artifact",
                                    ],
                                },
                            },
                            "additionalProperties": True,
                        },
                    },
                },
                "required": ["commands"],
            },
        ),
        handler=_whiteboard_draw_handler,
        toolset="whiteboard",
    )


async def _whiteboard_draw_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Validate the command list and acknowledge.

    Returns a compact ack rather than echoing the payload: the commands
    are already in the event log as the call's arguments, and echoing
    them back would double their cost in the next turn's replay.
    """
    commands = arguments.get("commands")

    # Some models serialise the array as a JSON string.  Recover
    # transparently -- the shape is unambiguous and a retry buys nothing.
    if isinstance(commands, str):
        try:
            commands = json.loads(commands)
        except json.JSONDecodeError as exc:
            return (
                f"Error: commands is a malformed JSON string "
                f"({exc.msg} at position {exc.pos}). Pass it as an array, "
                f"not a string."
            )

    error = validate_commands(commands)
    if error:
        return f"Error: {error}"

    count = len(commands)
    logger.info("whiteboard_draw accepted %d command(s)", count)
    return (
        f"Drew {count} object{'s' if count != 1 else ''} on the canvas"
        f"{_placement_summary(commands)}. "
        f"They are now the user's active selection, so they can move, "
        f"resize or delete them. "
        f"The attached image and the occupied-cell list were captured "
        f"before this call and do not show it. Anything else you draw "
        f"this turn must keep clear of the position above, and if this "
        f"was the answer, stop -- do not draw it again."
    )


def _placement_summary(commands: list[Any]) -> str:
    """Where the commands landed, for the model's own benefit.

    The image and the occupancy list both date from before the call, so
    without this a second iteration has no evidence its first draw
    happened: same picture, same free cells, same acknowledgement. One
    real turn drew the same formula at the same coordinates twice and
    piled four answers on one spot.
    """
    spots: list[str] = []
    for cmd in commands:
        if not isinstance(cmd, dict):
            continue
        x, y = cmd.get("x"), cmd.get("y")
        if not (_is_number(x) and _is_number(y)):
            continue
        what = cmd.get("text") or cmd.get("latex") or cmd.get("tool") or ""
        label = str(what).strip().replace("\n", " ")
        if len(label) > 40:
            label = f"{label[:39]}…"
        # Quoted plainly, not with repr: repr doubles every backslash,
        # which turns a LaTeX command into something the model has to
        # unescape before it can recognise its own output.
        spots.append(f'"{label}" at ({x}, {y})' if label else f"({x}, {y})")
    if not spots:
        return ""
    return f": {'; '.join(spots)}"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
