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
    CANVAS_SIZE,
    MAX_COMMANDS,
    validate_commands,
)

logger = logging.getLogger(__name__)

WHITEBOARD_TOOL_NAMES: frozenset[str] = frozenset({"whiteboard_draw"})

_DESCRIPTION = (
    "Draw on the shared whiteboard canvas. Every coordinate is a global "
    f"logical coordinate on a {CANVAS_SIZE}x{CANVAS_SIZE} canvas -- never "
    "an image coordinate. Use the geometry in the turn's canvas note to "
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
        f"Drew {count} object{'s' if count != 1 else ''} on the canvas. "
        f"They are now the user's active selection, so they can move, "
        f"resize or delete them."
    )
