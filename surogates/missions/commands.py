"""Slash command parsing + handlers for /mission.

The harness loop calls :func:`parse_mission_command` with the args
substring (everything after ``/mission``). Returns a
:class:`MissionCommand` dataclass that the handlers consume.

Parsing is the only concern of this top section; the DB-writing
handlers (``handle_mission_create``, ``handle_mission_status``,
``handle_mission_pause``, ``handle_mission_resume``,
``handle_mission_cancel``) live below.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


MissionAction = Literal["create", "status", "pause", "resume", "cancel"]


class MissionCommandParseError(ValueError):
    """Raised when /mission args cannot be parsed."""


@dataclass(slots=True)
class MissionCommand:
    """Parsed shape of a /mission invocation."""

    action: MissionAction
    description: str | None = None
    rubric: str | None = None
    reason: str | None = None
    cascade_to_workers: bool = False


_CONTROL_VERBS = ("status", "pause", "resume", "cancel")
_RUBRIC_RE = re.compile(r"\bRubric\s*:", re.IGNORECASE)


def parse_mission_command(raw: str) -> MissionCommand:
    """Parse the args portion of a /mission slash command.

    Empty string → status (matches /goal's behaviour).

    A control verb (``status`` / ``pause`` / ``resume`` / ``cancel``)
    optionally followed by a free-form reason → that action with the
    reason captured. ``cancel --cascade [reason]`` sets
    ``cascade_to_workers=True``.

    Anything else is treated as a ``create`` invocation; it MUST contain
    a ``Rubric:`` block (case-insensitive), otherwise the parse fails.
    """
    text = (raw or "").strip()

    if not text:
        return MissionCommand(action="status")

    first_token, _, rest = text.partition(" ")
    verb = first_token.lower()
    if verb in _CONTROL_VERBS:
        rest = rest.strip()
        if verb == "cancel":
            cascade = False
            if rest.startswith("--cascade"):
                cascade = True
                rest = rest[len("--cascade"):].strip()
            return MissionCommand(
                action="cancel",
                reason=rest or None,
                cascade_to_workers=cascade,
            )
        if verb == "pause":
            return MissionCommand(action="pause", reason=rest or None)
        if verb == "resume":
            return MissionCommand(action="resume")
        return MissionCommand(action="status")

    match = _RUBRIC_RE.search(text)
    if match is None:
        raise MissionCommandParseError(
            "missing Rubric: block. Format: "
            "'/mission <description>\\n\\nRubric:\\n<criterion>'"
        )
    description = text[:match.start()].strip()
    rubric = text[match.end():].lstrip(": \n").strip()
    if not description:
        raise MissionCommandParseError("missing description before Rubric: block")
    if not rubric:
        raise MissionCommandParseError("Rubric: block is empty")
    return MissionCommand(
        action="create", description=description, rubric=rubric,
    )
