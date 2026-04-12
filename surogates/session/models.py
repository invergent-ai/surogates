"""Pydantic domain models for sessions, events, and leases.

These are *not* SQLAlchemy ORM models (those live in ``surogates.db.models``).
They are plain Pydantic schemas used throughout the application layer and are
constructable from SQLAlchemy rows via ``model_config = {"from_attributes": True}``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field


class Session(BaseModel):
    """Snapshot of a session's metadata and aggregate counters."""

    model_config = {"from_attributes": True}

    id: UUID
    user_id: UUID
    org_id: UUID
    channel: str
    status: str
    title: str | None = None
    model: str | None = None
    config: dict = Field(default_factory=dict)
    parent_id: UUID | None = None
    message_count: int = 0
    tool_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: Decimal = Decimal("0")
    created_at: datetime
    updated_at: datetime


class Event(BaseModel):
    """A single entry in the append-only event log.

    ``id`` is ``None`` before the event has been persisted (i.e. before the
    database assigns a ``BIGSERIAL`` value).  ``trace_id`` and ``span_id``
    link the event to a distributed trace for end-to-end observability.
    """

    model_config = {"from_attributes": True}

    id: int | None = None
    session_id: UUID
    type: str
    data: dict = Field(default_factory=dict)
    trace_id: str | None = None
    span_id: str | None = None
    created_at: datetime | None = None


class SessionLease(BaseModel):
    """Proof that a specific worker owns the exclusive right to run a session's harness."""

    model_config = {"from_attributes": True}

    session_id: UUID
    owner_id: str
    lease_token: UUID
    expires_at: datetime
