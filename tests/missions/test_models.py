"""Unit tests for Pydantic Mission + EventType extensions."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest


def test_pydantic_mission_constructible_from_orm_attributes():
    """Pydantic Mission constructs from a duck-typed row via from_attributes."""
    from surogates.missions.models import Mission as PydMission

    fake = type("FakeRow", (), {
        "id": uuid.uuid4(),
        "org_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "agent_id": "orchestrator",
        "description": "train model",
        "rubric": "gsm8k >= 0.8",
        "status": "active",
        "iteration": 0,
        "max_iterations": 20,
        "last_evaluation_result": None,
        "last_evaluation_explanation": None,
        "last_evaluation_feedback": None,
        "last_evaluation_at": None,
        "evaluator_parse_failures": 0,
        "paused_reason": None,
        "cancelled_reason": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    })()
    pyd = PydMission.model_validate(fake)
    assert pyd.status == "active"
    assert pyd.iteration == 0


def test_pydantic_mission_rejects_unknown_status():
    """Status is constrained to the documented state machine."""
    from pydantic import ValidationError

    from surogates.missions.models import Mission as PydMission

    with pytest.raises(ValidationError):
        PydMission(
            id=uuid.uuid4(), org_id=uuid.uuid4(), user_id=uuid.uuid4(),
            session_id=uuid.uuid4(), agent_id="a",
            description="g", rubric="r",
            status="bogus", iteration=0, max_iterations=20,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )


def test_event_types_include_mission_events():
    """EventType enum exposes the 7 mission lifecycle events."""
    from surogates.session.events import EventType

    assert EventType.MISSION_DEFINED.value == "mission.defined"
    assert EventType.MISSION_EVALUATION_START.value == "mission.evaluation.start"
    assert EventType.MISSION_EVALUATION_END.value == "mission.evaluation.end"
    assert EventType.MISSION_CONTINUATION.value == "mission.continuation"
    assert EventType.MISSION_PAUSED.value == "mission.paused"
    assert EventType.MISSION_RESUMED.value == "mission.resumed"
    assert EventType.MISSION_CANCELLED.value == "mission.cancelled"
