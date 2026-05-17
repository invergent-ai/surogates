"""Pydantic domain model for Task — mirrors the SQLAlchemy ORM Task row.

Used throughout the application layer. Constructible from a
``surogates.db.models.Task`` row via ``model_config = {"from_attributes": True}``.

Status values are tightly constrained: the seven values below are the
entire state machine; anything else is rejected by Pydantic validation.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


TaskStatus = Literal[
    "todo",
    "ready",
    "running",
    "blocked",
    "done",
    "failed",
    "cancelled",
]
"""Type alias mirroring the spec's status state machine.

Owners by transition:

* ``todo``       — set on insert when one or more parents are not yet done.
* ``ready``      — set by the dispatcher tick when all parents reach done,
                   or eagerly by ``spawn_task`` when ``parents=[]``.
* ``running``    — set by the dispatcher (or eager spawn) when a Session
                   attempt has been created and ``current_session_id`` is set.
* ``blocked``    — set by the ``task_block`` self-tool. Does not consume
                   a retry attempt.
* ``done``       — set by the dispatcher tick when the current Session
                   ends with a ``WORKER_COMPLETE`` event.
* ``failed``     — set by the dispatcher tick when ``attempt_count``
                   reaches ``max_attempts`` after a crash or timeout.
* ``cancelled``  — set by the ``cancel_task`` tool (only the spawning
                   parent session may call it).
"""


class Task(BaseModel):
    """Snapshot of a Task row, suitable for crossing the persistence boundary."""

    model_config = {"from_attributes": True}

    id: UUID
    org_id: UUID
    parent_session_id: UUID
    agent_def_name: str | None = None
    goal: str
    context: str | None = None
    current_session_id: UUID | None = None
    status: TaskStatus
    result: str | None = None
    result_metadata: dict | None = None
    blocked_reason: str | None = None
    attempt_count: int = 0
    max_attempts: int = 3
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
