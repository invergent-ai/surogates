"""Structured routing judge helpers for final-response user-action rescue."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from surogates.harness.structured_output import generate_structured

_USER_ACTION_RESCUE_SYSTEM: str = (
    "You are a strict routing judge for an agent harness. Decide whether "
    "the assistant's draft response is ending the turn while it is "
    "genuinely blocked on user input or user action. Default to "
    "action_kind='none' unless the assistant has clearly stopped a "
    "concrete in-progress task that cannot proceed without specific input "
    "from the user. When in doubt, choose 'none'. "
    "Use action_kind='ask_user_question' ONLY when the assistant has paused a "
    "specific in-progress task and is asking a specific question whose "
    "answer is required to continue. Set 'question' to that concise "
    "question. "
    "Use action_kind='action_required' when the assistant has paused for "
    "the user to perform a UI action it cannot do itself: login, MFA, "
    "OAuth, CAPTCHA, consent screen, file picker, or browser approval. "
    "Set 'instructions' to what the user must do and 'target' to 'browser' "
    "or 'session'. "
    "Use action_kind='none' for: completed work with polite closings ('let "
    "me know if you need anything else', 'feel free to ask'), status "
    "reports, summaries, recaps of what was done, suggestions, optional "
    "follow-ups ('I can also do X if you want'), rhetorical questions, "
    "offers to continue, and any case where the assistant could simply "
    "stop and wait without losing progress. A polite invitation to "
    "continue is NOT a blocker. Asking 'anything else?' is NOT a blocker. "
    "Return only JSON with keys: action_kind string, reason string, "
    "question string, title string, instructions string, context string, "
    "action_type string, target string."
)
_DYNAMIC_LOOP_EXCLUDED_TOOLS: frozenset[str] = frozenset({
    "cron_create",
    "cron_delete",
    "cron_list",
})


class _UserActionRescueDecision(BaseModel):
    action_kind: str = Field(
        default="none",
        description="One of none, ask_user_question, or action_required.",
    )
    needs_ask_user_question: bool = Field(
        default=False,
        description="Whether the assistant draft is blocked on user input.",
    )
    reason: str = Field(
        description="Short machine-readable reason for the routing decision.",
    )
    question: str = Field(
        default="",
        description="Concise question to ask via ask_user_question when blocked.",
    )
    context: str = Field(
        default="",
        description="Short context explaining why the user input is needed.",
    )
    title: str = Field(default="", description="Short inbox item title.")
    instructions: str = Field(
        default="",
        description="Instructions for a user action_required inbox item.",
    )
    action_type: str = Field(
        default="manual",
        description="Machine-readable action type such as browser or approval.",
    )
    target: str = Field(
        default="session",
        description="Where the user should perform the action.",
    )


async def _generate_user_action_rescue_structured(
    *,
    llm_client: Any,
    model: str,
    messages: list[dict[str, str]],
) -> dict[str, Any] | None:
    """Return a typed user-action rescue judge decision when supported."""
    decision = await generate_structured(
        llm_client=llm_client,
        model=model,
        messages=messages,
        output_model=_UserActionRescueDecision,
        max_tokens=300,
        temperature=0,
    )
    return decision.model_dump() if decision is not None else None
