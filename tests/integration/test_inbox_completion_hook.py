"""Inbox hook tests for session completion."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import pytest
from sqlalchemy import select

from surogates.db.models import InboxItem
from surogates.harness.budget import IterationBudget
from surogates.harness.loop import AgentHarness
from surogates.tools.registry import ToolRegistry

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


@dataclass
class _StubTenant:
    org_id: UUID
    user_id: UUID


async def test_complete_session_emits_inbox_task_complete(
    session_store,
    session_factory,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id="test-agent",
    )
    await session_store.update_session_title_if_empty(
        session.id,
        "Migrate users",
    )
    session = await session_store.get_session(session.id)
    lease = await session_store.try_acquire_lease(
        session.id,
        "worker-complete-test",
        ttl_seconds=60,
    )
    assert lease is not None

    harness = AgentHarness(
        session_store=session_store,
        tool_registry=ToolRegistry(),
        llm_client=object(),
        tenant=_StubTenant(org_id=org_id, user_id=user_id),
        worker_id="worker-complete-test",
        budget=IterationBudget(max_total=10),
        context_compressor=object(),
        prompt_builder=object(),
    )

    await harness._complete_session(
        session,
        [{"role": "assistant", "content": "All done."}],
        lease,
        reason="completed",
    )

    async with session_factory() as db:
        row = (
            await db.execute(
                select(InboxItem).where(InboxItem.session_id == session.id)
            )
        ).scalar_one()

    assert row.kind == "task_complete"
    assert row.title == "Migrate users"
    assert row.body == "All done."
    assert row.payload["outcome"] == "success"
    assert row.payload["summary"] == "All done."
