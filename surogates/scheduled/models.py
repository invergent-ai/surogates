from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ScheduledSession(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    org_id: UUID
    user_id: UUID
    agent_id: str
    name: str
    prompt: str
    schedule: dict = Field(default_factory=dict)
    schedule_display: str
    timezone: str = "UTC"
    status: str
    source: str
    repeat_limit: int | None = None
    run_count: int = 0
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_session_id: UUID | None = None
    last_error: str | None = None
    locked_by: str | None = None
    locked_until: datetime | None = None
    expires_at: datetime | None = None
    created_from_session_id: UUID | None = None
    created_at: datetime
    updated_at: datetime
