"""Shared fixtures for mission integration tests."""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest_asyncio

from surogates.db.models import Event, Session as ORMSession

from tests.integration.conftest import (
    create_org,
    create_user,
    issue_service_account_token,
)


@pytest_asyncio.fixture(loop_scope="session")
async def org_id(session_factory) -> uuid.UUID:
    return await create_org(session_factory)


@pytest_asyncio.fixture(loop_scope="session")
async def user_id(session_factory, org_id) -> uuid.UUID:
    return await create_user(session_factory, org_id)


@pytest_asyncio.fixture(loop_scope="session")
async def service_account_id(session_factory, org_id) -> uuid.UUID:
    """Create a real service-account row and return its UUID.

    Routes through :func:`issue_service_account_token` so the bcrypt
    ``token_hash`` and ``token_prefix`` NOT NULL columns get populated —
    inserting a bare ``ServiceAccount`` row fails the schema constraints.
    """
    issued = await issue_service_account_token(
        session_factory, org_id, name=f"missions-test-sa-{uuid.uuid4()}",
    )
    return issued.id


def _session_workspace_config(sid: uuid.UUID) -> dict:
    """Minimal config required by spawn_task's workspace-seeding helper.

    ``_spawn_task_handler`` validates these three fields before spawning
    a child session so the worker inherits a usable workspace. Mission
    handlers don't read them, but tests that exercise spawn_task on a
    mission-bearing session must provide them.
    """
    return {
        "storage_bucket": "test-bucket",
        "storage_key_prefix": f"test/{sid}",
        "workspace_path": f"/workspace/test/{sid}",
        "supports_vision": False,
    }


@pytest_asyncio.fixture(loop_scope="session")
async def chat_session(session_factory, org_id, user_id):
    """A fresh chat session (web channel) per test, with workspace config."""
    sid = uuid.uuid4()
    async with session_factory() as db:
        s = ORMSession(
            id=sid, org_id=org_id, user_id=user_id, agent_id="orchestrator",
            channel="web", status="active",
            config=_session_workspace_config(sid),
        )
        db.add(s)
        await db.commit()
        await db.refresh(s)
    return s


@pytest_asyncio.fixture(loop_scope="session")
async def sa_chat_session(session_factory, org_id, service_account_id):
    """A service-account-owned chat session (api channel), workspace config.

    Mirrors the shape produced by ``/v1/api/sessions`` in production: a
    session with ``user_id=NULL`` and ``service_account_id`` set.
    """
    sid = uuid.uuid4()
    async with session_factory() as db:
        s = ORMSession(
            id=sid, org_id=org_id, user_id=None,
            service_account_id=service_account_id,
            agent_id="orchestrator",
            channel="api", status="active",
            config=_session_workspace_config(sid),
        )
        db.add(s)
        await db.commit()
        await db.refresh(s)
    return s


async def a_mission_agent(
    session_factory, coordinator, mission_id, *, status: str = "completed", description: str = "Work",
    agent_type: str | None = None, report: str | None = None, updated_at: datetime | None = None,
    tokens: tuple[int, int] = (0, 0),
) -> ORMSession:
    """An agent *coordinator* started for mission *mission_id*: a child session on the delegation channel whose
    ``agent_dispatch`` names the mission, in *status*, with *report* as its last response."""
    sid = uuid.uuid4()
    config = {
        "agent_dispatch": {
            "description": description, "mode": "background", "fork": False, "parent": str(coordinator.id),
            "mission_id": str(mission_id),
        },
    }
    if agent_type:
        config["agent_type"] = agent_type
    async with session_factory() as db:
        row = ORMSession(
            id=sid, org_id=coordinator.org_id, user_id=coordinator.user_id,
            service_account_id=coordinator.service_account_id, agent_id=coordinator.agent_id,
            parent_id=coordinator.id, channel="delegation", status=status, config=config,
            input_tokens=tokens[0], output_tokens=tokens[1],
        )
        if updated_at is not None:
            row.created_at = row.updated_at = updated_at
        db.add(row)
        await db.flush()
        if report is not None:
            db.add(Event(session_id=sid, type="llm.response",
                         data={"message": {"role": "assistant", "content": report}}))
        await db.commit()
        await db.refresh(row)
    return row
