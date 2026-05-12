"""Outcome-oriented goal state and prompt helpers.

This module is intentionally pure: no database, Redis, or harness imports.
The API route and harness loop persist state and enqueue work around these
helpers.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any
from uuid import uuid4

DEFAULT_MAX_ITERATIONS = 3
MAX_MAX_ITERATIONS = 20
DEFAULT_OUTCOME_RUBRIC = (
    "The outcome is satisfied only when the assistant's latest response "
    "explicitly confirms the requested work is complete, clearly presents "
    "the final deliverable, or clearly explains that the work is blocked or "
    "unachievable and what remains outside the agent's control."
)

_RUBRIC_RE = re.compile(r"\n\s*(?:rubric|criteria)\s*:\s*\n", re.IGNORECASE)


@dataclass(frozen=True)
class OutcomeCommand:
    action: str
    text: str = ""
    rubric: str = ""


@dataclass
class OutcomeState:
    id: str
    description: str
    rubric: str
    status: str = "active"
    iteration: int = 0
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    created_at: str = ""
    updated_at: str = ""
    last_result: str | None = None
    last_explanation: str | None = None
    last_feedback: str | None = None
    paused_reason: str | None = None
    consecutive_parse_failures: int = 0

    def to_config(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_config(cls, data: Any) -> "OutcomeState | None":
        if not isinstance(data, dict):
            return None
        description = str(data.get("description") or "").strip()
        if not description:
            return None
        return cls(
            id=str(data.get("id") or f"outc_{uuid4().hex}"),
            description=description,
            rubric=str(data.get("rubric") or DEFAULT_OUTCOME_RUBRIC),
            status=str(data.get("status") or "active"),
            iteration=_coerce_int(data.get("iteration"), 0),
            max_iterations=_clamp_max_iterations(data.get("max_iterations")),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            last_result=_coerce_optional_str(data.get("last_result")),
            last_explanation=_coerce_optional_str(data.get("last_explanation")),
            last_feedback=_coerce_optional_str(data.get("last_feedback")),
            paused_reason=_coerce_optional_str(data.get("paused_reason")),
            consecutive_parse_failures=_coerce_int(
                data.get("consecutive_parse_failures"),
                0,
            ),
        )


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _clamp_max_iterations(value: Any) -> int:
    parsed = _coerce_int(value, DEFAULT_MAX_ITERATIONS)
    return min(MAX_MAX_ITERATIONS, max(1, parsed))


def parse_goal_command(args: str) -> OutcomeCommand:
    text = (args or "").strip()
    lower = text.lower()
    if not text or lower == "status":
        return OutcomeCommand(action="status")
    if lower in {"pause", "resume", "clear"}:
        return OutcomeCommand(action=lower)
    match = _RUBRIC_RE.search(text)
    if match is None:
        return OutcomeCommand(action="set", text=text, rubric="")
    return OutcomeCommand(
        action="set",
        text=text[: match.start()].strip(),
        rubric=text[match.end() :].strip(),
    )


def start_outcome(
    description: str,
    *,
    rubric: str,
    max_iterations: int,
    now_iso: str,
) -> OutcomeState:
    cleaned = (description or "").strip()
    if not cleaned:
        raise ValueError("goal text is empty")
    return OutcomeState(
        id=f"outc_{uuid4().hex}",
        description=cleaned,
        rubric=(rubric or "").strip() or DEFAULT_OUTCOME_RUBRIC,
        max_iterations=_clamp_max_iterations(max_iterations),
        created_at=now_iso,
        updated_at=now_iso,
    )


def build_continuation_prompt(state: OutcomeState) -> str:
    feedback = (
        state.last_feedback
        or state.last_explanation
        or "Continue with the next concrete revision."
    )
    return (
        "[Continuing toward your defined outcome]\n"
        f"Outcome: {state.description}\n\n"
        f"Rubric:\n{state.rubric}\n\n"
        f"Evaluator feedback:\n{feedback}\n\n"
        "Revise the work to satisfy the outcome. Take the next concrete step. "
        "If the outcome is now satisfied, state that explicitly and stop. "
        "If you are blocked and need user input, say so clearly and stop."
    )
