"""Structured ambient-post decision + the deterministic post gate."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AmbientPostDecision(BaseModel):
    action: str = Field(description="'post' to post a message, 'none' to stay silent")
    target_thread: str = Field(default="", description="Slack thread_ts to reply in, or '' for channel")
    confidence: float = Field(ge=0.0, le=1.0, description="0-1 confidence this is worth posting")
    message: str = Field(default="", description="The message to post when action='post'")
    rationale: str = Field(default="", description="Why this is or isn't worth posting")


def gate_decision(
    decision: AmbientPostDecision,
    *,
    confidence_threshold: float,
    limiter_ok: bool,
) -> bool:
    """Deterministic gate: confidence is necessary but never sufficient."""
    if decision.action != "post":
        return False
    if not decision.message or not decision.message.strip():
        return False
    if decision.confidence < confidence_threshold:
        return False
    return bool(limiter_ok)
