"""Unit tests for Pydantic domain models and event-type enum extensions.

These are pure unit tests (no DB) — DB-backed ORM round-trips live under
``tests/integration/tasks/test_models.py``.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone


def test_pydantic_session_has_task_id_field():
    """Pydantic Session domain model carries task_id."""
    from surogates.session.models import Session as PydSession

    s = PydSession(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        agent_id="a",
        channel="task",
        status="active",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        task_id=uuid.uuid4(),
    )
    assert s.task_id is not None


def test_pydantic_session_task_id_defaults_to_none():
    """When task_id isn't provided, the field defaults to None (plain chat session)."""
    from surogates.session.models import Session as PydSession

    s = PydSession(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        agent_id="a",
        channel="web",
        status="active",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    assert s.task_id is None


def test_event_types_include_task_events():
    """EventType enum exposes TASK_BLOCKED and TASK_FAILED."""
    from surogates.session.events import EventType

    assert EventType.TASK_BLOCKED.value == "task.blocked"
    assert EventType.TASK_FAILED.value == "task.failed"
