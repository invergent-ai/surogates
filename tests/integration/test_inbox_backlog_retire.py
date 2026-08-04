"""Retiring the "task complete" rows nothing can ever clear.

The emission rule now refuses to raise these for sessions with no
conversation to open, but the ones already in the table outlive the fix:
382 pending rows in the development database, the oldest five weeks old.
The statement drains them and, because its predicate is exactly "a row
the current rule would never create", stays a no-op afterwards.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from surogates.db.inbox_backlog import RETIRE_UNREACHABLE_INBOX_SQL
from surogates.db.models import InboxItem
from surogates.session.events import EventType

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _session_on(
    session_store,
    session_factory,
    *,
    channel: str,
    parent_id=None,
    config: dict | None = None,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    return await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id="test-agent",
        channel=channel,
        parent_id=parent_id,
        config=config,
    )


async def _emit(session_store, session_id, event_type, data):
    event_id = await session_store.emit_event(session_id, event_type, data)
    async with session_store._sf() as db:
        return (
            await db.execute(
                select(InboxItem).where(InboxItem.source_event_id == event_id)
            )
        ).scalar_one()


async def _emit_task_complete(session_store, session_id):
    return await _emit(
        session_store,
        session_id,
        EventType.INBOX_TASK_COMPLETE,
        {
            "outcome": "success",
            "summary": "All done.",
            "duration_seconds": 1,
            "session_title": "Task complete",
        },
    )


async def _retire(session_store) -> None:
    async with session_store._sf() as db:
        await db.execute(text(RETIRE_UNREACHABLE_INBOX_SQL))
        await db.commit()


async def _status_of(session_store, item_id: int) -> str:
    async with session_store._sf() as db:
        return (await db.get(InboxItem, item_id)).status


async def test_retires_items_no_surface_can_clear(session_store, session_factory):
    parent = await _session_on(session_store, session_factory, channel="web")
    subagent = await _session_on(
        session_store, session_factory, channel="worker", parent_id=parent.id,
    )
    automation = await _session_on(session_store, session_factory, channel="api")
    messaging = await _session_on(session_store, session_factory, channel="slack")

    orphans = [
        await _emit_task_complete(session_store, subagent.id),
        await _emit_task_complete(session_store, automation.id),
        await _emit_task_complete(session_store, messaging.id),
    ]

    await _retire(session_store)

    for item in orphans:
        assert await _status_of(session_store, item.id) == "expired"


async def test_leaves_items_a_person_can_still_open(session_store, session_factory):
    conversation = await _session_on(session_store, session_factory, channel="web")
    scheduled = await _session_on(
        session_store,
        session_factory,
        channel="scheduled",
        config={"scheduled_session_id": "11111111-1111-1111-1111-111111111111"},
    )

    kept = [
        await _emit_task_complete(session_store, conversation.id),
        await _emit_task_complete(session_store, scheduled.id),
    ]

    await _retire(session_store)

    for item in kept:
        assert await _status_of(session_store, item.id) == "pending"


async def test_touches_neither_answered_items_nor_questions(
    session_store,
    session_factory,
):
    automation = await _session_on(session_store, session_factory, channel="api")
    question = await _emit(
        session_store,
        automation.id,
        EventType.INBOX_INPUT_REQUIRED,
        {
            "tool_call_id": "tc-retire-1",
            "questions": [{"prompt": "Which one?"}],
            "context": "",
        },
    )
    settled = await _emit_task_complete(session_store, automation.id)
    await session_store.mark_inbox_read(
        item_id=settled.id, user_id=automation.user_id, agent_id="test-agent",
    )

    await _retire(session_store)

    # A question still needs its answer, whatever raised it, and a row
    # already resolved is history — neither is this statement's business.
    assert await _status_of(session_store, question.id) == "pending"
    assert await _status_of(session_store, settled.id) == "acknowledged"


async def test_second_run_changes_nothing(session_store, session_factory):
    automation = await _session_on(session_store, session_factory, channel="api")
    item = await _emit_task_complete(session_store, automation.id)

    await _retire(session_store)
    async with session_store._sf() as db:
        first = (await db.get(InboxItem, item.id)).updated_at

    await _retire(session_store)
    async with session_store._sf() as db:
        second = (await db.get(InboxItem, item.id)).updated_at

    assert first == second
