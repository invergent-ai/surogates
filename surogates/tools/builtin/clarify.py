"""Clarify tool -- interactive clarifying questions.

Allows the agent to present structured multiple-choice questions or open-ended
prompts to the user. On the web channel, choices are rendered as buttons. On
messaging platforms, choices are rendered as a numbered list.

The actual user-interaction logic lives in the channel layer. This module
defines the schema, validation, and a thin dispatcher that delegates to a
platform-provided callback (injected via kwargs).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)

# Maximum number of predefined choices the agent can offer.
# A 5th "Other (type your answer)" option is always appended by the UI.
MAX_CHOICES = 4

CLARIFY_DESCRIPTION = (
    "Ask the user a question when you need clarification, feedback, or a "
    "decision before proceeding. Supports two modes:\n\n"
    "1. **Multiple choice** — provide up to 4 choices. The user picks one "
    "or types their own answer via a 5th 'Other' option.\n"
    "2. **Open-ended** — omit choices entirely. The user types a free-form "
    "response.\n\n"
    "Use this tool when:\n"
    "- The task is ambiguous and you need the user to choose an approach\n"
    "- You want post-task feedback ('How did that work out?')\n"
    "- You want to offer to save a skill or update memory\n"
    "- A decision has meaningful trade-offs the user should weigh in on\n\n"
    "Do NOT use this tool for simple yes/no confirmation of dangerous "
    "commands (the terminal tool handles that). Prefer making a reasonable "
    "default choice yourself when the decision is low-stakes."
)

CLARIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "The question to present to the user.",
        },
        "choices": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": MAX_CHOICES,
            "description": (
                "Up to 4 answer choices. Omit this parameter entirely to "
                "ask an open-ended question. When provided, the UI "
                "automatically appends an 'Other (type your answer)' option."
            ),
        },
    },
    "required": ["question"],
}


def clarify_tool(
    question: str,
    choices: list[str] | None = None,
    callback: Any | None = None,
) -> str:
    """Ask the user a question, optionally with multiple-choice options.

    Args:
        question: The question text to present.
        choices:  Up to 4 predefined answer choices. When omitted the
                  question is purely open-ended.
        callback: Platform-provided function that handles the actual UI
                  interaction. Signature: callback(question, choices) -> str.
                  Injected by the agent runner via kwargs.

    Returns:
        JSON string with the user's response.
    """
    if not question or not question.strip():
        return json.dumps({"error": "Question text is required."})

    question = question.strip()

    # Validate and trim choices.
    if choices is not None:
        if not isinstance(choices, list):
            return json.dumps({"error": "choices must be a list of strings."})
        choices = [str(c).strip() for c in choices if str(c).strip()]
        if len(choices) > MAX_CHOICES:
            choices = choices[:MAX_CHOICES]
        if not choices:
            choices = None  # empty list → open-ended

    if callback is None:
        return json.dumps(
            {"error": "Clarify tool is not available in this execution context."},
            ensure_ascii=False,
        )

    try:
        user_response = callback(question, choices)
    except Exception as exc:
        return json.dumps(
            {"error": f"Failed to get user input: {exc}"},
            ensure_ascii=False,
        )

    return json.dumps({
        "question": question,
        "choices_offered": choices,
        "user_response": str(user_response).strip(),
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Handler + registration
# ---------------------------------------------------------------------------


async def _clarify_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    """Async handler for the clarify tool."""
    return clarify_tool(
        question=arguments.get("question", ""),
        choices=arguments.get("choices"),
        callback=kwargs.get("clarify_callback"),
    )


def register(registry: ToolRegistry) -> None:
    """Register the clarify tool."""
    registry.register(
        name="clarify",
        schema=ToolSchema(
            name="clarify",
            description=CLARIFY_DESCRIPTION,
            parameters=CLARIFY_SCHEMA,
        ),
        handler=_clarify_handler,
        toolset="clarify",
    )
