"""SessionStore helpers for inbox API routes."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from surogates.db.models import InboxItem
from surogates.session.events import EventType

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _create_user_session(session_store, session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id="test-agent",
    )
    return session


async def _emit_task_complete(session_store, session_id):
    event_id = await session_store.emit_event(
        session_id,
        EventType.INBOX_TASK_COMPLETE,
        {
            "outcome": "success",
            "summary": "All done.",
            "duration_seconds": 1,
            "session_title": "Task complete",
        },
    )
    async with session_store._sf() as db:
        return (
            await db.execute(
                select(InboxItem).where(InboxItem.source_event_id == event_id)
            )
        ).scalar_one()


async def test_list_inbox_returns_items_for_user_only(
    session_store,
    session_factory,
):
    session = await _create_user_session(session_store, session_factory)
    other = await _create_user_session(session_store, session_factory)
    expected = await _emit_task_complete(session_store, session.id)
    await _emit_task_complete(session_store, other.id)

    rows = await session_store.list_inbox(
        user_id=session.user_id, agent_id="test-agent", limit=50,
    )

    assert [row.id for row in rows] == [expected.id]
    assert rows[0].user_id == session.user_id


async def test_mark_inbox_read_sets_read_at_idempotently(
    session_store,
    session_factory,
):
    session = await _create_user_session(session_store, session_factory)
    item = await _emit_task_complete(session_store, session.id)

    updated = await session_store.mark_inbox_read(
        item_id=item.id,
        user_id=session.user_id,
        agent_id="test-agent",
    )
    first_read_at = updated.read_at
    assert first_read_at is not None

    again = await session_store.mark_inbox_read(
        item_id=item.id,
        user_id=session.user_id,
        agent_id="test-agent",
    )
    assert again.read_at == first_read_at


async def _emit_question(session_store, session_id):
    event_id = await session_store.emit_event(
        session_id,
        EventType.INBOX_INPUT_REQUIRED,
        {
            "tool_call_id": "tc-read-1",
            "questions": [{"prompt": "Which one?"}],
            "context": "",
        },
    )
    async with session_store._sf() as db:
        return (
            await db.execute(
                select(InboxItem).where(InboxItem.source_event_id == event_id)
            )
        ).scalar_one()


async def test_reading_an_update_retires_it(session_store, session_factory):
    """An informational item is finished the moment it has been read.

    There is nothing else to do to a "task complete" notice, so leaving
    it pending kept it in the list forever and made the operator
    acknowledge each one by hand — hundreds of them, in practice.
    """
    session = await _create_user_session(session_store, session_factory)
    item = await _emit_task_complete(session_store, session.id)

    updated = await session_store.mark_inbox_read(
        item_id=item.id,
        user_id=session.user_id,
        agent_id="test-agent",
    )

    assert updated.read_at is not None
    assert updated.status == "acknowledged"
    assert updated.responded_at is not None


async def test_reading_an_update_twice_is_idempotent(
    session_store,
    session_factory,
):
    session = await _create_user_session(session_store, session_factory)
    item = await _emit_task_complete(session_store, session.id)

    first = await session_store.mark_inbox_read(
        item_id=item.id, user_id=session.user_id, agent_id="test-agent",
    )
    again = await session_store.mark_inbox_read(
        item_id=item.id, user_id=session.user_id, agent_id="test-agent",
    )

    assert again.read_at == first.read_at
    assert again.status == "acknowledged"


async def test_reading_a_question_leaves_it_pending(
    session_store,
    session_factory,
):
    """A question is not answered by being looked at."""
    session = await _create_user_session(session_store, session_factory)
    item = await _emit_question(session_store, session.id)

    updated = await session_store.mark_inbox_read(
        item_id=item.id,
        user_id=session.user_id,
        agent_id="test-agent",
    )

    assert updated.read_at is not None
    assert updated.status == "pending"
    assert updated.responded_at is None


async def test_set_inbox_status_rejects_terminal_transition(
    session_store,
    session_factory,
):
    session = await _create_user_session(session_store, session_factory)
    item = await _emit_task_complete(session_store, session.id)

    updated = await session_store.set_inbox_status(
        item_id=item.id,
        user_id=session.user_id,
        agent_id="test-agent",
        new_status="acknowledged",
    )
    assert updated.status == "acknowledged"
    assert updated.responded_at is not None

    with pytest.raises(ValueError):
        await session_store.set_inbox_status(
            item_id=item.id,
            user_id=session.user_id,
            agent_id="test-agent",
            new_status="responded",
        )
