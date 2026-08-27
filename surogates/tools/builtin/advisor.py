"""Built-in ``advisor`` tool -- executor-initiated consult of a stronger model.

The executor decides when to escalate: before committing to an approach,
when an error keeps recurring, or before declaring a task done. The
advisor reads the transcript and returns guidance as this tool's result.

Timing is deliberately model-driven. The harness used to classify every
user turn with a cheap LLM call and block the first request on the
verdict; that taxed every turn and almost never delivered guidance early
enough to matter. See :mod:`surogates.harness.loop_advisor`.

The handler needs ``advisor_consult`` -- a callable bound by the harness
loop to the live message list and system prompt -- injected as a keyword
argument by the tool registry dispatch.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from surogates.harness.expert_routing import HARD_TASK_CATEGORIES
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)

#: Categories the advisor prompt understands. Shared with the retired
#: classifier's vocabulary so ``advisor.*`` events stay groupable across
#: the change.
_CATEGORIES = sorted(HARD_TASK_CATEGORIES)

_ADVISOR_SCHEMA = ToolSchema(
    name="advisor",
    description=(
        "Consult a stronger advisor model for strategic guidance. It sees "
        "the conversation so far -- the task, your tool calls and their "
        "results -- and returns a plan, a correction, or a stop signal.\n"
        "Call it before substantive work (before writing, before "
        "committing to an interpretation), when stuck on a recurring "
        "error, and before declaring a task complete. Orientation "
        "(reading files, fetching a source) is not substantive work -- do "
        "that first, then consult, so the advisor has something to read.\n"
        "Give the advice serious weight, but adapt if a step fails when "
        "tried or direct evidence contradicts a specific claim."
    ),
    parameters={
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": _CATEGORIES,
                "description": "The kind of help you need",
            },
            "task": {
                "type": "string",
                "description": (
                    "What you are trying to do and where you are stuck. "
                    "The transcript is forwarded automatically -- do not "
                    "restate it here."
                ),
            },
        },
        "required": ["category", "task"],
        "additionalProperties": False,
    },
)


def register(registry: ToolRegistry) -> None:
    """Register the ``advisor`` tool."""
    registry.register(
        name="advisor",
        schema=_ADVISOR_SCHEMA,
        handler=_advisor_handler,
        toolset="advisor",
    )


async def _advisor_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Run one consult and return the guidance as the tool result."""
    advisor_consult = kwargs.get("advisor_consult")
    if advisor_consult is None:
        return json.dumps({
            "error": "No advisor is configured for this session.",
        })

    category = (arguments.get("category") or "").strip()
    task = (arguments.get("task") or "").strip()
    if not task:
        return json.dumps({"error": "No task provided"})
    if category not in _CATEGORIES:
        category = "problem_solving"

    try:
        guidance = await advisor_consult(category=category, task=task)
    except Exception as exc:
        # The consult already emits ADVISOR_FAILURE; keep the executor
        # moving rather than failing its turn on a second-opinion call.
        logger.warning("Advisor consult raised", exc_info=True)
        return json.dumps({"error": f"Advisor consult failed: {exc}"})

    if not guidance:
        # Budget spent, advisor unconfigured, or an upstream failure --
        # all of which mean "carry on by yourself", not "retry".
        return json.dumps({
            "status": "unavailable",
            "guidance": None,
            "note": (
                "No advisor guidance is available for this turn. Continue "
                "with your own judgement; do not call the advisor again "
                "this turn."
            ),
        })

    return json.dumps({
        "status": "ok",
        "category": category,
        "guidance": guidance,
    })
