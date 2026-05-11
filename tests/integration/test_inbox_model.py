"""Schema-level tests for InboxItem."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from surogates.db.models import InboxItem
from surogates.session.events import EventType

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _seed_session_and_event(session_store, session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id="test-agent",
    )
    event_id = await session_store.emit_event(
        session.id,
        EventType.INBOX_TASK_COMPLETE,
        {"title": "Task complete"},
    )
    return session, event_id


async def test_inbox_item_can_be_inserted(session_store, session_factory):
    session, event_id = await _seed_session_and_event(
        session_store, session_factory
    )

    async with session_factory() as db:
        item = InboxItem(
            org_id=session.org_id,
            user_id=session.user_id,
            session_id=session.id,
            source_event_id=event_id,
            kind="task_complete",
            title="Task complete",
            body="The work finished.",
            payload={"outcome": "success", "duration_seconds": 10},
        )
        db.add(item)

        await db.flush()

        assert item.id is not None
        assert item.status == "pending"
        assert item.payload == {"outcome": "success", "duration_seconds": 10}
        assert item.read_at is None
        assert item.responded_at is None


async def test_inbox_item_source_event_id_is_unique(
    session_store,
    session_factory,
):
    session, event_id = await _seed_session_and_event(
        session_store, session_factory
    )

    async with session_factory() as db:
        first = InboxItem(
            org_id=session.org_id,
            user_id=session.user_id,
            session_id=session.id,
            source_event_id=event_id,
            kind="task_complete",
            title="Task complete",
        )
        second = InboxItem(
            org_id=session.org_id,
            user_id=session.user_id,
            session_id=session.id,
            source_event_id=event_id,
            kind="task_complete",
            title="Duplicate task complete",
        )
        db.add_all([first, second])

        with pytest.raises(IntegrityError):
            await db.flush()
