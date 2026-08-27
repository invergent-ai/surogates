"""Whiteboard canvas chat surface.

Spec: docs/superpowers/specs/2026-08-27-whiteboard-canvas-chat-design.md
"""

from surogates.whiteboard.commands import (
    CANVAS_SIZE,
    MAX_COMMANDS,
    WHITEBOARD_TOOLS,
    validate_commands,
)

__all__ = [
    "CANVAS_SIZE",
    "MAX_COMMANDS",
    "WHITEBOARD_TOOLS",
    "validate_commands",
]
