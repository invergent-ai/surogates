"""The ``end_call`` builtin tool: a phone call's agent decides the conversation is over.

Offered only on voice sessions (``harness/tool_schemas.py``). The tool itself only records the
decision; the voice process (``surogates/voice``) sees the call in the event log and hangs up once
the agent's goodbye has been spoken.
"""

from __future__ import annotations

import json
from typing import Any

from surogates.tools.registry import ToolRegistry, ToolSchema

END_CALL_SCHEMA = ToolSchema(
    name="end_call",
    description=(
        "End this phone call. Use it when the caller says goodbye, says they need nothing else, or the "
        "conversation is clearly finished. After calling it, say one short goodbye sentence: the call "
        "hangs up as soon as it has been spoken."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "Why the call is ending, in a few words."},
        },
        "required": [],
    },
)


async def _end_call_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    return json.dumps({"success": True, "message": "The call ends after your reply: say goodbye in one short sentence."},
                      ensure_ascii=False)


def register(registry: ToolRegistry) -> None:
    """Register the end_call tool."""
    registry.register(name="end_call", schema=END_CALL_SCHEMA, handler=_end_call_handler, toolset="voice")
