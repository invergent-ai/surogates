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


def is_whiteboard_session(session: Any) -> bool:
    """Whether *session* is a whiteboard surface.

    ``getattr`` with a ``None`` guard: several harnesses build partial
    session objects that skip ``__init__``, and a missing config means
    "not a whiteboard", not a programming error.
    """
    config = getattr(session, "config", None)
    if not isinstance(config, dict):
        return False
    return config.get(SURFACE_KEY) == SURFACE_VALUE


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

    ``config.surface`` arrives from the client, and the harness force-adds
    ``whiteboard_draw`` for a whiteboard session *past* any AgentDef
    allowlist -- so without this an ordinary caller can hand themselves
    the canvas toolset on an agent whose operator switched the board off.
    That makes the surface a capability escalation, not a preference.

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
