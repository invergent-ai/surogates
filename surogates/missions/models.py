"""Pydantic domain model for Mission — mirrors the ORM row.

Used throughout the application layer. Constructible from a
``surogates.db.models.Mission`` row via
``model_config = {"from_attributes": True}``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


MissionStatus = Literal[
    "active",
    "paused",
    "satisfied",
    "blocked",
    "failed",
    "cancelled",
    "max_iterations_reached",
]
"""Type alias mirroring the spec's status state machine.

* ``active``                 — created, evaluator firing on triggers
* ``paused``                 — evaluator suspended; workers continue
* ``satisfied``              — terminal; rubric judge returned satisfied
* ``blocked``                — terminal; coordinator or judge marked blocked
* ``failed``                 — terminal; coordinator or judge marked failed
* ``cancelled``              — terminal; user cancelled (workers may still run
                               unless cascade_to_workers=True was passed)
* ``max_iterations_reached`` — terminal; bumped past max_iterations on
                               repeated needs_revision verdicts
"""


EvaluationResult = Literal[
    "satisfied",
    "needs_revision",
    "blocked",
    "failed",
]


class Mission(BaseModel):
    """Snapshot of a Mission row."""

    model_config = {"from_attributes": True}

    id: UUID
    org_id: UUID
    user_id: UUID
    session_id: UUID
    agent_id: str
    description: str
    rubric: str
    status: MissionStatus
    iteration: int = 0
    max_iterations: int = 20
    last_evaluation_result: EvaluationResult | None = None
    last_evaluation_explanation: str | None = None
    last_evaluation_feedback: str | None = None
    last_evaluation_at: datetime | None = None
    evaluator_parse_failures: int = 0
    paused_reason: str | None = None
    cancelled_reason: str | None = None
    created_at: datetime
    updated_at: datetime
