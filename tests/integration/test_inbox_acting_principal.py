"""Inbox items target the triggering sender, not the shared-session owner."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from surogates.db.models import InboxItem
from surogates.session.events import EventType
from tests.integration.conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_inbox_targets_the_actual_sender(session_store, session_factory):
    org_id = await create_org(session_factory)
    owner = await create_user(session_factory, org_id)
    sender = await create_user(session_factory, org_id)
    sid = uuid4()
    await session_store.create_session(
        session_id=sid,
        user_id=owner,
        org_id=org_id,
        agent_id="a1",
        channel="slack",
        config={},
    )

    # A real user message from `sender` sets the acting principal.
    await session_store.emit_event(sid, EventType.USER_MESSAGE, {
        "content": "do the thing", "principal_user_id": str(sender),
    })
    # A task-complete inbox event fired during that turn.
    await session_store.emit_event(sid, EventType.INBOX_TASK_COMPLETE, {
        "outcome": "success",
        "summary": "All done.",
        "duration_seconds": 3,
        "session_title": "Shared thread task",
    })

    async with session_factory() as db:
        item = (await db.execute(
            select(InboxItem).where(InboxItem.session_id == sid)
        )).scalar_one()
    assert item.user_id == sender  # not the owner
