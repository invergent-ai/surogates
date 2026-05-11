"""SessionStore.emit_event inbox mirroring tests."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from surogates.db.models import InboxItem
from surogates.session.events import EventType
from surogates.session.store import SessionStore

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


class RecordingRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str) -> None:
        self.published.append((channel, message))


async def _create_user_session(session_store, session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    return await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id="test-agent",
    )


async def test_inbox_row_written_for_inbox_event_type(
    session_store,
    session_factory,
):
    session = await _create_user_session(session_store, session_factory)

    event_id = await session_store.emit_event(
        session.id,
        EventType.INBOX_TASK_COMPLETE,
        {
            "outcome": "success",
            "summary": "All done.",
            "duration_seconds": 7,
            "session_title": "Migrate users",
        },
    )

    async with session_factory() as db:
        row = (
            await db.execute(
                select(InboxItem).where(InboxItem.source_event_id == event_id)
            )
        ).scalar_one()

    assert row.kind == "task_complete"
    assert row.user_id == session.user_id
    assert row.status == "pending"
    assert row.payload["outcome"] == "success"


async def test_no_inbox_row_for_non_inbox_event(
    session_store,
    session_factory,
):
    session = await _create_user_session(session_store, session_factory)

    event_id = await session_store.emit_event(
        session.id,
        EventType.LLM_RESPONSE,
        {"message": {"role": "assistant", "content": "hi"}},
    )

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(InboxItem).where(InboxItem.source_event_id == event_id)
            )
        ).scalars().all()

    assert rows == []


async def test_userless_session_skips_inbox(session_store, session_factory):
    org_id = await create_org(session_factory)
    session = await session_store.create_session(
        user_id=None,
        org_id=org_id,
        agent_id="test-agent",
    )

    event_id = await session_store.emit_event(
        session.id,
        EventType.INBOX_TASK_COMPLETE,
        {
            "outcome": "success",
            "duration_seconds": 1,
            "summary": "No user should be notified.",
        },
    )

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(InboxItem).where(InboxItem.source_event_id == event_id)
            )
        ).scalars().all()

    assert rows == []


async def test_inbox_event_publishes_post_commit_user_nudge(session_factory):
    redis = RecordingRedis()
    store = SessionStore(session_factory, redis=redis)
    session = await _create_user_session(store, session_factory)

    event_id = await store.emit_event(
        session.id,
        EventType.INBOX_TASK_COMPLETE,
        {
            "outcome": "success",
            "summary": "All done.",
            "duration_seconds": 7,
            "session_title": "Migrate users",
        },
    )

    async with session_factory() as db:
        item = (
            await db.execute(
                select(InboxItem).where(InboxItem.source_event_id == event_id)
            )
        ).scalar_one()

    assert (
        f"surogates:inbox:{session.user_id}",
        f"{item.id}:task_complete",
    ) in redis.published
