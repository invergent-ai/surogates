import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.tools.builtin.loop_control import (
    _loop_complete_handler,
    _loop_wait_handler,
)


class FakeStore:
    def __init__(self) -> None:
        self.finished: list[dict] = []
        self.completed: list[dict] = []

    async def mark_dynamic_run_finished(self, **kwargs):
        self.finished.append(kwargs)
        return True

    async def mark_loop_completed(self, **kwargs):
        self.completed.append(kwargs)
        return True


@pytest.mark.asyncio
async def test_loop_wait_requires_dynamic_loop_session():
    result = json.loads(await _loop_wait_handler(
        {"delay_seconds": 120, "reason": "waiting for CI"},
        tenant=SimpleNamespace(org_id=uuid4(), user_id=uuid4(), service_account_id=None),
        agent_id="agent-a",
        session_id=str(uuid4()),
        session_config={},
        scheduled_store=FakeStore(),
    ))

    assert result["success"] is False
    assert "dynamic loop" in result["error"]


@pytest.mark.asyncio
async def test_loop_wait_clamps_and_persists_next_delay():
    org_id = uuid4()
    user_id = uuid4()
    schedule_id = uuid4()
    session_id = uuid4()
    store = FakeStore()

    result = json.loads(await _loop_wait_handler(
        {"delay_seconds": 5, "reason": "fast retry"},
        tenant=SimpleNamespace(org_id=org_id, user_id=user_id, service_account_id=None),
        agent_id="agent-a",
        session_id=str(session_id),
        session_config={
            "scheduled_session_id": str(schedule_id),
            "scheduled_dynamic_loop": True,
        },
        scheduled_store=store,
    ))

    assert result["success"] is True
    assert result["delay_seconds"] == 60
    assert result["completed"] is False
    assert store.finished == [{
        "schedule_id": schedule_id,
        "org_id": org_id,
        "user_id": user_id,
        "service_account_id": None,
        "agent_id": "agent-a",
        "session_id": session_id,
        "delay_seconds": 60,
        "reason": "fast retry",
        "completed": False,
    }]


@pytest.mark.asyncio
async def test_loop_wait_passes_completed_flag_to_store():
    org_id = uuid4()
    user_id = uuid4()
    schedule_id = uuid4()
    session_id = uuid4()
    store = FakeStore()

    result = json.loads(await _loop_wait_handler(
        {"delay_seconds": 3600, "reason": "task done", "completed": True},
        tenant=SimpleNamespace(org_id=org_id, user_id=user_id, service_account_id=None),
        agent_id="agent-a",
        session_id=str(session_id),
        session_config={
            "scheduled_session_id": str(schedule_id),
            "scheduled_dynamic_loop": True,
        },
        scheduled_store=store,
    ))

    assert result["success"] is True
    assert result["completed"] is True
    assert store.finished[0]["completed"] is True


@pytest.mark.asyncio
async def test_loop_wait_rejects_invalid_schedule_id():
    result = json.loads(await _loop_wait_handler(
        {"delay_seconds": 120, "reason": "waiting"},
        tenant=SimpleNamespace(org_id=uuid4(), user_id=uuid4(), service_account_id=None),
        agent_id="agent-a",
        session_id=str(uuid4()),
        session_config={
            "scheduled_session_id": "not-a-uuid",
            "scheduled_dynamic_loop": True,
        },
        scheduled_store=FakeStore(),
    ))

    assert result["success"] is False
    assert "Invalid dynamic loop id" in result["error"]


@pytest.mark.asyncio
async def test_loop_complete_marks_schedule_completed():
    org_id = uuid4()
    user_id = uuid4()
    schedule_id = uuid4()
    session_id = uuid4()
    store = FakeStore()

    result = json.loads(await _loop_complete_handler(
        {"reason": "Collected 2 BTC prices as requested"},
        tenant=SimpleNamespace(org_id=org_id, user_id=user_id, service_account_id=None),
        agent_id="agent-a",
        session_id=str(session_id),
        session_config={
            "scheduled_session_id": str(schedule_id),
            "scheduled_dynamic_loop": False,
        },
        scheduled_store=store,
    ))

    assert result["success"] is True
    assert store.completed == [{
        "schedule_id": schedule_id,
        "org_id": org_id,
        "user_id": user_id,
        "service_account_id": None,
        "agent_id": "agent-a",
        "session_id": session_id,
        "reason": "Collected 2 BTC prices as requested",
    }]


@pytest.mark.asyncio
async def test_loop_complete_rejects_dynamic_loop():
    """Dynamic loops should use loop_wait(completed=true), not loop_complete."""
    result = json.loads(await _loop_complete_handler(
        {"reason": "done"},
        tenant=SimpleNamespace(org_id=uuid4(), user_id=uuid4(), service_account_id=None),
        agent_id="agent-a",
        session_id=str(uuid4()),
        session_config={
            "scheduled_session_id": str(uuid4()),
            "scheduled_dynamic_loop": True,
        },
        scheduled_store=FakeStore(),
    ))

    assert result["success"] is False
    assert "loop_wait" in result["error"]


@pytest.mark.asyncio
async def test_loop_complete_rejects_non_scheduled_session():
    result = json.loads(await _loop_complete_handler(
        {"reason": "done"},
        tenant=SimpleNamespace(org_id=uuid4(), user_id=uuid4(), service_account_id=None),
        agent_id="agent-a",
        session_id=str(uuid4()),
        session_config={},
        scheduled_store=FakeStore(),
    ))

    assert result["success"] is False
    assert "/loop run" in result["error"]


@pytest.mark.asyncio
async def test_loop_complete_requires_reason():
    result = json.loads(await _loop_complete_handler(
        {"reason": "   "},
        tenant=SimpleNamespace(org_id=uuid4(), user_id=uuid4(), service_account_id=None),
        agent_id="agent-a",
        session_id=str(uuid4()),
        session_config={
            "scheduled_session_id": str(uuid4()),
            "scheduled_dynamic_loop": False,
        },
        scheduled_store=FakeStore(),
    ))

    assert result["success"] is False
    assert "reason is required" in result["error"]
