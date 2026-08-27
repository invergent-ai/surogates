"""Predicates identifying a whiteboard session and a whiteboard turn.

Kept in one module so the harness loop, the worker's prompt-surface
filter, the schema-surface filter and the prompt builder all ask the same
question the same way.  Scattering ``config.get("surface")`` literals is
how the equivalent channel checks drifted.
"""
from __future__ import annotations

from typing import Any

#: The ``session.config`` key and value that mark a whiteboard session.
#: Deliberately a *surface* within the web/studio channels rather than a
#: new ``channel`` value -- see the design doc's "Session shape".
SURFACE_KEY = "surface"
SURFACE_VALUE = "whiteboard"

#: The cheap single-round-trip mode.  Any unrecognised value resolves
#: here: ``mode`` arrives from the client, so an unknown string must
#: never be able to promote a turn to the full tool catalogue.
MODE_SKETCH = "sketch"
MODE_DEEP = "deep"


#: First line of the geometry note attached to every whiteboard turn.
#: Doubles as the marker that identifies a canvas message during replay:
#: the canvas arrives as an ordinary image attachment, so without it the
#: pruner cannot tell one from a screenshot the user uploaded.
CANVAS_NOTE_HEADER = "The user is working on a whiteboard canvas."


def is_whiteboard_turn(metadata: Any) -> bool:
    """Whether *this turn* was sent from the canvas.

    The board is a view mode, not a session type: the same session
    alternates freely between message turns and canvas turns, so the
    question can only be answered per turn.  Presence of the metadata
    block is the answer -- the client attaches it exactly when it
    attaches a canvas render.
    """
    return whiteboard_metadata(metadata) is not None


def whiteboard_metadata(metadata: Any) -> dict[str, Any] | None:
    """Return the ``whiteboard`` block of a message's metadata, or ``None``."""
    if not isinstance(metadata, dict):
        return None
    payload = metadata.get("whiteboard")
    return payload if isinstance(payload, dict) else None


def turn_mode(metadata: Any) -> str:
    """Return this turn's mode: :data:`MODE_SKETCH` or :data:`MODE_DEEP`."""
    payload = whiteboard_metadata(metadata)
    if payload is None:
        return MODE_SKETCH
    return MODE_DEEP if payload.get("mode") == MODE_DEEP else MODE_SKETCH


def surface_rejection(
    config: Any,
    *,
    whiteboard_enabled: bool,
) -> str | None:
    """Reject a session config asking for a surface the agent lacks.

    The surface no longer decides the tool set -- the agent's board
    capability does, because the canvas is a view mode the user can
    reach from any session.  What is left is a plain input check on a
    client-supplied value: an unimplemented surface is a request the
    server cannot honour, and asking for a board on an agent that has
    none is better answered with a 403 than with a canvas that silently
    never draws.

    Returns an error message, or ``None`` when the config is acceptable.
    Pure so it can be tested without a request; the route turns a
    non-``None`` result into a 403.
    """
    if not isinstance(config, dict):
        return None
    surface = config.get(SURFACE_KEY)
    if surface is None:
        return None
    if surface != SURFACE_VALUE:
        return (
            f"Unknown session surface {surface!r}. "
            f"Valid surfaces: {SURFACE_VALUE}."
        )
    if not whiteboard_enabled:
        return (
            "This agent does not have the whiteboard capability enabled."
        )
    return None
