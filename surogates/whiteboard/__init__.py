"""Whiteboard canvas chat surface.

Spec: docs/superpowers/specs/2026-08-27-whiteboard-canvas-chat-design.md
"""

from surogates.whiteboard.commands import (
    COORD_LIMIT,
    MAX_COMMANDS,
    WHITEBOARD_TOOLS,
    validate_commands,
)
from surogates.whiteboard.session import (
    MODE_DEEP,
    MODE_SKETCH,
    SURFACE_KEY,
    SURFACE_VALUE,
    is_whiteboard_session,
    turn_mode,
    whiteboard_metadata,
)

__all__ = [
    "COORD_LIMIT",
    "MAX_COMMANDS",
    "MODE_DEEP",
    "MODE_SKETCH",
    "SURFACE_KEY",
    "SURFACE_VALUE",
    "WHITEBOARD_TOOLS",
    "is_whiteboard_session",
    "turn_mode",
    "validate_commands",
    "whiteboard_metadata",
]
