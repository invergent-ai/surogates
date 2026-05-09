import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.tools.builtin.cron import (
    _cron_create_handler,
    _cron_delete_handler,
    _cron_list_handler,
)


class FakeStore:
    def __init__(self):
        self.created = []
        self.deleted = []

    async def create(self, **kwargs):
        row = SimpleNamespace(
            id=uuid4(),
            name=kwargs["name"],
            prompt=kwargs["prompt"],
            schedule_display=kwargs["schedule"].display,
            next_run_at=kwargs.get("next_run_at"),
            status="active",
        )
        self.created.append(kwargs)
        return row

    async def list_for_user(self, **kwargs):
        return [
            SimpleNamespace(
                id=uuid4(),
                name="Deploy check",
                prompt="check deploy",
                schedule_display="Every 10 minutes",
                next_run_at=None,
                status="active",
            ),
        ]

    async def delete(self, **kwargs):
        self.deleted.append(kwargs)
        return True


@pytest.mark.asyncio
async def test_cron_create_requires_user_context():
    result = json.loads(await _cron_create_handler(
        {"cron": "*/10 * * * *", "prompt": "check"},
        tenant=SimpleNamespace(org_id=uuid4(), user_id=None),
        scheduled_store=FakeStore(),
    ))
    assert result["success"] is False
    assert "user-owned" in result["error"]


@pytest.mark.asyncio
async def test_cron_create_success():
    tenant = SimpleNamespace(org_id=uuid4(), user_id=uuid4())
    store = FakeStore()
    result = json.loads(await _cron_create_handler(
        {
            "cron": "*/10 * * * *",
            "prompt": "check deploy",
            "recurring": True,
        },
        tenant=tenant,
        agent_id="agent-a",
        session_id=str(uuid4()),
        scheduled_store=store,
    ))
    assert result["success"] is True
    assert result["schedule"]["prompt"] == "check deploy"
    assert store.created[0]["agent_id"] == "agent-a"
    assert store.created[0]["source"] == "tool"


@pytest.mark.asyncio
async def test_cron_list_returns_user_schedules():
    tenant = SimpleNamespace(org_id=uuid4(), user_id=uuid4())
    result = json.loads(await _cron_list_handler(
        {},
        tenant=tenant,
        agent_id="agent-a",
        scheduled_store=FakeStore(),
    ))
    assert result["success"] is True
    assert result["schedules"][0]["name"] == "Deploy check"


@pytest.mark.asyncio
async def test_cron_delete_removes_user_schedule():
    tenant = SimpleNamespace(org_id=uuid4(), user_id=uuid4())
    store = FakeStore()
    schedule_id = uuid4()
    result = json.loads(await _cron_delete_handler(
        {"id": str(schedule_id)},
        tenant=tenant,
        agent_id="agent-a",
        scheduled_store=store,
    ))
    assert result["success"] is True
    assert store.deleted[0]["schedule_id"] == schedule_id
