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
from typing import Any, Literal
from uuid import UUID

from surogates.config import enqueue_session
from surogates.db.models import Session as ORMSession
from surogates.missions.store import (
    ActiveMissionConflictError,
    MissionStore,
)
from surogates.session.events import EventType


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


# ---------------------------------------------------------------------------
# Slash command handlers
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MissionHandlerResult:
    """Standard return shape for slash handlers."""

    ok: bool
    mission_id: UUID | None = None
    message: str = ""
    error: str = ""


_KICKOFF_TEMPLATE = """\
[Mission kickoff]

Description: {description}

Rubric:
{rubric}

You are this mission's coordinator. Decompose into specialist sub-agent
tasks via ``spawn_task``; gate dependencies with ``parents=[...]``;
end criterion-driven rounds with a verifier task whose
``result_metadata`` carries the measurable signal the rubric checks.

Do NOT claim completion in prose alone — the evaluator only honours
completion claims backed by verifier-task evidence OR an explicit
``[[mission-complete]]`` marker on its own line in your response.
"""


def _outcome_is_active(outcome: dict[str, Any] | None) -> bool:
    """True iff session.config['outcome'] represents a non-terminal /goal."""
    if not isinstance(outcome, dict):
        return False
    status = outcome.get("status")
    return status in ("active", "paused")


async def handle_mission_create(
    *,
    description: str,
    rubric: str,
    session_id: UUID,
    user_id: UUID,
    org_id: UUID,
    agent_id: str,
    session_store: Any,
    session_factory: Any,
    mission_store: MissionStore,
    redis: Any,
) -> MissionHandlerResult:
    """Create a new mission on the calling session.

    Rejects when the session already has:
    * a non-terminal /goal outcome (``session.config['outcome']`` in
      active/paused), OR
    * an active or paused mission (via :class:`MissionStore`).

    On success: inserts the Mission row, updates session.config with
    ``active_mission_id``, ``coordinator=True``, and the
    ``subagent-task-orchestrator`` preloaded skill; emits
    ``mission.defined``; emits a synthetic ``user.message`` with the
    kickoff prompt; enqueues the session for immediate processing.
    """
    async with session_factory() as db:
        sess = await db.get(ORMSession, session_id)
        if sess is None:
            return MissionHandlerResult(
                ok=False, error=f"session {session_id} not found",
            )
        if _outcome_is_active(sess.config.get("outcome")):
            return MissionHandlerResult(
                ok=False,
                error=(
                    "This session has an active /goal. Clear or pause it "
                    "before starting a /mission (only one evaluator loop "
                    "per session is allowed)."
                ),
            )

    try:
        mission_id = await mission_store.create(
            org_id=org_id, user_id=user_id, session_id=session_id,
            agent_id=agent_id, description=description, rubric=rubric,
        )
    except ActiveMissionConflictError as exc:
        return MissionHandlerResult(ok=False, error=str(exc))

    async with session_factory() as db:
        sess = await db.get(ORMSession, session_id)
        cfg = dict(sess.config or {})
        cfg["active_mission_id"] = str(mission_id)
        cfg["coordinator"] = True
        preloaded = list(cfg.get("preloaded_skills") or [])
        if "subagent-task-orchestrator" not in preloaded:
            preloaded.append("subagent-task-orchestrator")
        cfg["preloaded_skills"] = preloaded
        sess.config = cfg
        await db.commit()

    await session_store.emit_event(
        session_id, EventType.MISSION_DEFINED,
        {
            "mission_id": str(mission_id),
            "description": description,
            "rubric": rubric,
            "max_iterations": 20,
        },
    )
    kickoff = _KICKOFF_TEMPLATE.format(description=description, rubric=rubric)
    await session_store.emit_event(
        session_id, EventType.USER_MESSAGE,
        {"content": kickoff, "synthetic": "mission_kickoff"},
    )

    await enqueue_session(redis, agent_id, session_id)

    return MissionHandlerResult(
        ok=True, mission_id=mission_id,
        message=f"Mission {mission_id} started.",
    )
