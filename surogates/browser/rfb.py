"""RFB ClientMessage type gating for live-view WebSocket proxying."""

from __future__ import annotations

RFB_INPUT_TYPES: frozenset[int] = frozenset({4, 5, 6})


def is_input_frame(frame: bytes) -> bool:
    """Return true when an RFB ClientMessage requires user-control access."""
    if not frame:
        return False
    return frame[0] in RFB_INPUT_TYPES
