"""Per-turn whiteboard facts the tool handler needs but is not passed.

The harness loop knows the turn's marks (they ride on the user message);
the ``whiteboard_draw`` handler only sees the call's arguments.  The
handler has to reject a call that leaves the user's slot empty *before*
the client folds it -- a rejected call is skipped on the board, an
accepted one is drawn -- so the loop publishes the slot labels here for
the duration of the wake.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any

#: Labels of the slots the user reserved this turn, e.g. ``{"S1"}``.
current_slots: ContextVar[frozenset[str]] = ContextVar(
    "whiteboard_current_slots", default=frozenset(),
)


def slots_from_metadata(metadata: Any) -> frozenset[str]:
    """The slot labels in a turn's ``whiteboard`` metadata block."""
    from surogates.whiteboard.session import whiteboard_metadata

    payload = whiteboard_metadata(metadata)
    if payload is None:
        return frozenset()
    marks = payload.get("marks")
    if not isinstance(marks, list):
        return frozenset()
    return frozenset(
        m["id"]
        for m in marks
        if isinstance(m, dict) and m.get("kind") == "slot"
        and isinstance(m.get("id"), str)
    )
