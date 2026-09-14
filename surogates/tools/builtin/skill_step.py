"""Step markers for procedure skills.

A graph-backed skill compiles into a numbered procedure whose preamble
asks the model to call ``skill_step`` before and after each step. Each
call appends one ``skill.step`` event; the event log is the record and
the Studio canvas derives the path from it. The tool keeps no state and
does not read the skill's graph: during a dry run the runtime only holds
the published bundle, so the draft's step ids could not be checked here
without rejecting the runs the canvas exists for.
"""
from __future__ import annotations

import json
import re
from typing import Any

from surogates.session.events import EventType
from surogates.tools.registry import ToolRegistry, ToolSchema

STATUSES = ("started", "completed", "skipped")
STEP_ID = re.compile(r"^s\d{1,7}$")

DESCRIPTION = (
    "Record where you are in a numbered procedure skill. Call it with the "
    "skill's name, the step id from the step heading (such as `s3`), and "
    "`started` before you begin a step or `completed` when it is done, in "
    "the same response as the step's own work. Mark the steps on branches "
    "you do not take `skipped`. Returns a confirmation; it never changes "
    "what the procedure says."
)


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="skill_step",
        schema=ToolSchema(
            name="skill_step",
            description=DESCRIPTION,
            parameters={
                "type": "object",
                "properties": {
                    "skill": {
                        "type": "string",
                        "description": "The skill name given in the procedure preamble.",
                    },
                    "step": {
                        "type": "string",
                        "description": "The step id in parentheses in the step heading, such as s3.",
                    },
                    "status": {
                        "type": "string",
                        "enum": list(STATUSES),
                        "description": "started before the step, completed after it, skipped for a branch not taken.",
                    },
                },
                "required": ["skill", "step", "status"],
            },
        ),
        handler=_skill_step_handler,
        toolset="skills",
    )


def _error(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)


async def _skill_step_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    skill = str(arguments.get("skill") or "").strip()
    step = str(arguments.get("step") or "").strip()
    status = str(arguments.get("status") or "").strip().lower()
    if not skill:
        return _error("skill is required: the skill name from the procedure preamble.")
    if len(skill) > 200:
        return _error("skill must be at most 200 characters.")
    if not STEP_ID.match(step):
        return _error("step must be the id from the step heading, such as s3.")
    if status not in STATUSES:
        return _error("status must be one of started, completed or skipped.")
    session_id = kwargs.get("session_id")
    session_store = kwargs.get("session_store")
    if session_store is None or not session_id:
        return _error("There is no session to record the step in.")
    # Not swallowed on purpose: a marker that was not written must fail
    # loudly, or the replay would silently miss the step.
    await session_store.emit_event(
        session_id,
        EventType.SKILL_STEP,
        {"skill": skill, "step": step, "status": status, "tool_call_id": kwargs.get("tool_call_id")},
    )
    return json.dumps({"ok": True, "skill": skill, "step": step, "status": status}, ensure_ascii=False)
