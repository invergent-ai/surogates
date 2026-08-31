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
    unfilled_slots,
    validate_commands,
    validate_intent,
    validate_readings,
)
from surogates.whiteboard.turn import current_slots

logger = logging.getLogger(__name__)

WHITEBOARD_TOOL_NAMES: frozenset[str] = frozenset({"whiteboard_draw"})

_DESCRIPTION = (
    "Draw on the shared whiteboard canvas.\n\n"
    "PLACE BY RELATION, NOT BY COORDINATES. Name what your answer "
    "relates to and which side it goes on; the client computes position "
    "and size against the live board:\n"
    "- {tool:'draw_formula', latex, anchor:'latest', side:'right'} -- "
    "the answer, right of what the user just wrote.\n"
    "- {tool:'write_text', text, anchor:'latest', side:'below'} -- an "
    "explanation under it, sized to read.\n"
    "- {tool:'draw_formula', latex, replaces:'<call id>'} -- a revision: "
    "takes the replaced object's place, the old one is removed.\n"
    "- {tool:'write_text', text, anchor:'S1', side:'in'} -- FILL A SLOT: "
    "the user drew an empty box (S1, S2, ...) where the result goes; "
    "side 'in' fits your content into it and the box disappears. A "
    "slot is the user's instruction: fill every slot before anything "
    "else, with the thing that belongs there -- the missing letter, the "
    "result, the requested sketch -- never with a question about it. "
    "Only what is missing: after '1/x dx =' the slot takes 'ln|x| + C', "
    "not the whole equation restated.\n"
    "Anchors: a label from the image and turn note -- A1, A2, ... are "
    "the user's ink, B1, B2, ... are your own earlier objects, S1, S2, "
    "... are slots -- or 'latest' (the user's newest ink) or "
    "'selection' (their lasso). Sides: right (default), below, above, "
    "left, in (slots only). replaces takes a B-label or a call id. Anchored commands omit x/y/fontSize/maxWidth; "
    "a formula or short answer is sized to the anchor's handwriting, "
    "prose gets a readable size and width.\n\n"
    "Commands:\n"
    "- write_text {tool,text,anchor?,side?|x,y,fontSize,maxWidth,"
    "lineHeight?} -- prose.\n"
    "- draw_formula {tool,latex,anchor?,side?|x,y,fontSize} -- "
    "mathematical notation.\n"
    "- draw {tool,origin:[x,y],types:[...],items:[[...]],width?,tension?,"
    "closed?,fill?,arrows?} -- a simple sketch or annotation of about ten "
    "or fewer primitives. types and items must be the same length. "
    "Encodings: line/smooth [x1,y1,x2,y2,...]; rect [x,y,w,h]; ellipse "
    "[cx,cy,rx,ry]; circle [cx,cy,r]; arc [cx,cy,rx,ry,startDeg,sweepDeg]. "
    "Item coordinates are integer offsets from origin. Into a slot "
    "(anchor:'S1', side:'in') omit origin and draw in a 1000x1000 local "
    "box that the client scales to the slot; elsewhere sketches are "
    "absolute.\n"
    "- erase {tool,mode:'rect',x,y,w,h} or {tool,mode:'path',points,size} "
    "-- for the user's ink only. Never erase your own objects: to remove "
    "or change one, draw the replacement with replaces:'B1' (or just "
    "replaces it with nothing to draw: an erase over it is treated as a "
    "removal).\n"
    "- place_artifact {tool,artifact_id,w,h,anchor?,side?|x,y} -- "
    "position an artifact you already created with create_artifact. For "
    "anything larger, richer, or interactive than a simple sketch -- a "
    "chart, a diagram, a table, a widget -- create an artifact and place "
    "it rather than approximating it with many draw commands.\n\n"
    "Explicit x/y (global canvas units, freely negative, never image "
    "pixels) always wins over an anchor: it is the escape hatch for "
    "placements no relation describes.\n\n"
    "INTENT. Say what you are doing with intent: 'fill' (a slot), "
    "'continue' (extend what the user wrote -- after the =, the next "
    "line, the next stroke), 'transform' (the user drew an operation "
    "around your object: brackets, an exponent, a strike-through), or "
    "'respond' (prose, only when asked). If you can produce the thing, "
    "produce it on the board; ask only when you cannot.\n\n"
    "READINGS PERSIST. Alongside commands, pass readings: "
    "[{mark:'A2', text:'2x + 1 = 7'}] -- your transcription of each ink "
    "mark you read this turn. It is stored with that ink and handed back "
    "to you as text on every later turn, so a mark whose note entry "
    "already says what it reads is settled: trust it, do not re-read "
    "its pixels. Transcribe only marks without a reading (the NEW ones, "
    "typically). Plain text or LaTeX, one line, exactly what is written "
    "-- not your answer to it.\n\n"
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
                    "intent": {
                        "type": "string",
                        "enum": ["fill", "continue", "transform", "respond"],
                        "description": (
                            "What this call does: fill a slot, continue "
                            "the user's work, transform your own object "
                            "as their ink asks, or respond in prose."
                        ),
                    },
                    "readings": {
                        "type": "array",
                        "description": (
                            "Your transcription of each ink mark you read "
                            "this turn, {mark, text}. Stored with the ink "
                            "and returned as text on later turns."
                        ),
                        "items": {
                            "type": "object",
                            "required": ["mark", "text"],
                            "properties": {
                                "mark": {"type": "string"},
                                "text": {"type": "string"},
                            },
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
    readings = arguments.get("readings")
    error = validate_readings(readings)
    if error:
        return f"Error: {error}"
    error = validate_intent(arguments.get("intent"))
    if error:
        return f"Error: {error}"
    # A slot is the user's own answer to "where does the result go".
    # Rejected here, before the client folds anything: an accepted call
    # is drawn, a rejected one is skipped, and a retry that fills the
    # slot must not land beside an earlier answer that missed it.
    empty = unfilled_slots(commands, current_slots.get())
    if empty:
        first = empty[0]
        return (
            f"Error: {first} is an empty slot the user drew for your "
            f"answer, and this call leaves it empty. Put what belongs "
            f"there into it -- anchor:'{first}', side:'in' -- before "
            f"anything else. If the slot needs something you cannot "
            f"produce, fill it with one short line saying what you need."
        )

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
        f"{_readings_ack(readings)}"
    )


def _readings_ack(readings: Any) -> str:
    if not isinstance(readings, list) or not readings:
        return ""
    marks = [
        str(r.get("mark")).strip()
        for r in readings
        if isinstance(r, dict) and isinstance(r.get("mark"), str)
    ]
    marks = [m for m in marks if m]
    if not marks:
        return ""
    return (
        f" Recorded your reading of {', '.join(marks)}; it will come back "
        f"to you as text from now on."
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
        what = cmd.get("text") or cmd.get("latex") or cmd.get("tool") or ""
        # Quoted plainly, not with repr: repr doubles every backslash,
        # which turns a LaTeX command into something the model has to
        # unescape before it can recognise its own output.
        label = str(what).strip().replace("\n", " ")
        if len(label) > 40:
            label = f"{label[:39]}…"
        named = f'"{label}"' if label else "an object"
        x, y = cmd.get("x"), cmd.get("y")
        anchor = cmd.get("anchor")
        replaces = cmd.get("replaces")
        if _is_number(x) and _is_number(y):
            spots.append(f"{named} at ({x}, {y})")
        elif isinstance(anchor, str) and anchor.strip():
            if cmd.get("side") == "in":
                spots.append(f"{named} filling {anchor}")
            else:
                spots.append(f"{named} placed {cmd.get('side') or 'right'} of {anchor}")
        elif isinstance(replaces, str) and replaces.strip():
            spots.append(f"{named} in place of call {replaces}")
    if not spots:
        return ""
    return f": {'; '.join(spots)}"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
