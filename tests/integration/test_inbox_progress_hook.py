"""Inbox hook tests for harness progress check-ins."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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


async def _setup_harness(session_store, session_factory, *, interval):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id="test-agent",
        config={"inbox_checkin_interval_seconds": interval},
    )
    session.created_at = datetime.now(timezone.utc) - timedelta(seconds=120)
    harness = AgentHarness(
        session_store=session_store,
        tool_registry=ToolRegistry(),
        llm_client=object(),
        tenant=_StubTenant(org_id=org_id, user_id=user_id),
        worker_id="worker-progress-test",
        budget=IterationBudget(max_total=10),
        context_compressor=object(),
        prompt_builder=object(),
    )
    return harness, session


async def test_progress_checkin_emitted_after_interval(
    session_store,
    session_factory,
):
    harness, session = await _setup_harness(
        session_store,
        session_factory,
        interval=60,
    )

    await harness._maybe_emit_progress_checkin(
        session,
        [{"role": "assistant", "content": "Indexed 1,200 files."}],
        iteration_count=3,
        last_tool="shell_exec",
    )

    async with session_factory() as db:
        row = (
            await db.execute(
                select(InboxItem).where(InboxItem.session_id == session.id)
            )
        ).scalar_one()

    assert row.kind == "progress_checkin"
    assert row.body == "Indexed 1,200 files."
    assert row.payload["iterations"] == 3
    assert row.payload["last_tool"] == "shell_exec"


async def test_progress_checkin_skipped_when_disabled(
    session_store,
    session_factory,
):
    harness, session = await _setup_harness(
        session_store,
        session_factory,
        interval=None,
    )

    await harness._maybe_emit_progress_checkin(
        session,
        [{"role": "assistant", "content": "Indexed 1,200 files."}],
        iteration_count=3,
        last_tool="shell_exec",
    )

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(InboxItem).where(InboxItem.session_id == session.id)
            )
        ).scalars().all()

    assert rows == []
