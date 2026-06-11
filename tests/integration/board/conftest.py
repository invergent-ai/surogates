"""Fixtures for coordination-board integration tests.

Reuses the shared testcontainers engine/session_factory from
``tests/integration/conftest.py``; adds the same ``org_id`` /
``parent_session`` fixtures the task-layer tests use.
"""
from __future__ import annotations

import uuid

import pytest_asyncio

from surogates.db.models import Session as ORMSession

from tests.integration.conftest import create_org


@pytest_asyncio.fixture(loop_scope="session")
async def org_id(session_factory) -> uuid.UUID:
    return await create_org(session_factory)


@pytest_asyncio.fixture(loop_scope="session")
async def parent_session(session_factory, org_id: uuid.UUID) -> ORMSession:
    pid = uuid.uuid4()
    async with session_factory() as db:
        s = ORMSession(
            id=pid, org_id=org_id, agent_id="orchestrator",
            channel="web", status="active",
            config={
                "storage_bucket": "test-bucket",
                "storage_key_prefix": "",
                "workspace_path": f"/workspace/test/{pid}",
                "supports_vision": False,
            },
        )
        db.add(s)
        await db.commit()
        await db.refresh(s)
    return s
