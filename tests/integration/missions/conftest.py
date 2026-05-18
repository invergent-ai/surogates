"""Shared fixtures for mission integration tests."""
from __future__ import annotations

import uuid

import pytest_asyncio

from surogates.db.models import Session as ORMSession

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
