"""Shared fixtures for mission integration tests."""
from __future__ import annotations

import uuid

import pytest_asyncio

from surogates.db.models import Session as ORMSession

from tests.integration.conftest import create_org, create_user


@pytest_asyncio.fixture(loop_scope="session")
async def org_id(session_factory) -> uuid.UUID:
    return await create_org(session_factory)


@pytest_asyncio.fixture(loop_scope="session")
async def user_id(session_factory, org_id) -> uuid.UUID:
    return await create_user(session_factory, org_id)


@pytest_asyncio.fixture(loop_scope="session")
async def chat_session(session_factory, org_id, user_id):
    """A fresh chat session (web channel) per test."""
    sid = uuid.uuid4()
    async with session_factory() as db:
        s = ORMSession(
            id=sid, org_id=org_id, user_id=user_id, agent_id="orchestrator",
            channel="web", status="active",
        )
        db.add(s)
        await db.commit()
        await db.refresh(s)
    return s
