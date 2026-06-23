"""Sweeper tests for expiring inbox items on terminal sessions."""

from __future__ import annotations

import pytest
from sqlalchemy import select, update

from surogates.db.models import InboxItem, Session
from surogates.jobs.inbox_expire import expire_inbox_items
from surogates.session.events import EventType

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _create_user_session(session_factory, session_store):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    return await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id="test-agent",
    )


async def _get_inbox_item_for_session(session_store, session_id) -> InboxItem:
    async with session_store._sf() as db:
        return (
            await db.execute(
                select(InboxItem).where(InboxItem.session_id == session_id)
            )
        ).scalar_one()


async def test_sweeper_expires_pending_items_for_terminal_sessions(
    session_factory,
    session_store,
):
    session = await _create_user_session(session_factory, session_store)
    await session_store.emit_event(
        session.id,
        EventType.INBOX_INPUT_REQUIRED,
        {
            "tool_call_id": "tc-expire",
            "questions": [{"prompt": "Continue?"}],
            "context": "",
        },
    )
    async with session_store._sf() as db:
        await db.execute(
            update(Session)
            .where(Session.id == session.id)
            .values(status="completed")
        )
        await db.commit()

    expired_count = await expire_inbox_items(session_store)
    item = await _get_inbox_item_for_session(session_store, session.id)

    assert expired_count >= 1
    assert item.status == "expired"


async def test_sweeper_does_not_touch_active_sessions(
    session_factory,
    session_store,
):
    session = await _create_user_session(session_factory, session_store)
    await session_store.emit_event(
        session.id,
        EventType.INBOX_INPUT_REQUIRED,
        {
            "tool_call_id": "tc-active",
            "questions": [{"prompt": "Continue?"}],
            "context": "",
        },
    )

    expired_count = await expire_inbox_items(session_store)
    item = await _get_inbox_item_for_session(session_store, session.id)

    assert expired_count == 0
    assert item.status == "pending"


async def test_sweeper_does_not_expire_acknowledge_only_kinds(
    session_factory,
    session_store,
):
    """task_complete / progress_checkin are acknowledge-only (informational):
    they have nothing to act on against a live session, so terminal-session
    expiry must not touch them, unlike input_required."""
    session = await _create_user_session(session_factory, session_store)
    await session_store.emit_event(
        session.id,
        EventType.INBOX_INPUT_REQUIRED,
        {
            "tool_call_id": "tc-q",
            "questions": [{"prompt": "Continue?"}],
            "context": "",
        },
    )
    await session_store.emit_event(
        session.id,
        EventType.INBOX_TASK_COMPLETE,
        {"outcome": "success", "summary": "All done."},
    )
    async with session_store._sf() as db:
        await db.execute(
            update(Session)
            .where(Session.id == session.id)
            .values(status="completed")
        )
        await db.commit()

    await expire_inbox_items(session_store)

    async with session_store._sf() as db:
        rows = (
            await db.execute(
                select(InboxItem).where(InboxItem.session_id == session.id)
            )
        ).scalars().all()
    by_kind = {r.kind: r.status for r in rows}
    assert by_kind["input_required"] == "expired"
    assert by_kind["task_complete"] == "pending"


async def test_sweeper_does_not_touch_responded_items(
    session_factory,
    session_store,
):
    session = await _create_user_session(session_factory, session_store)
    await session_store.emit_event(
        session.id,
        EventType.INBOX_INPUT_REQUIRED,
        {
            "tool_call_id": "tc-responded",
            "questions": [{"prompt": "Continue?"}],
            "context": "",
        },
    )
    item = await _get_inbox_item_for_session(session_store, session.id)
    await session_store.set_inbox_status(
        item_id=item.id,
        user_id=session.user_id,
        new_status="responded",
    )
    async with session_store._sf() as db:
        await db.execute(
            update(Session)
            .where(Session.id == session.id)
            .values(status="completed")
        )
        await db.commit()

    expired_count = await expire_inbox_items(session_store)
    item = await _get_inbox_item_for_session(session_store, session.id)

    assert expired_count == 0
    assert item.status == "responded"
