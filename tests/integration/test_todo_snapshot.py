"""``latest_todo_snapshot`` against real PostgreSQL.

The unit tests drive the handler with a fake store, so the actual SQL is
never executed there. The handler receives ``session_id`` as a **string**
(``tool_exec`` passes ``session_id=str(session.id)``) while ``events.session_id``
is a UUID column -- if that comparison does not coerce, the query silently
returns nothing and the plan is lost in production while every unit test
passes.
"""

from __future__ import annotations

import uuid

import pytest

from surogates.session.events import EventType

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _session(session_store, session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    return await session_store.create_session(
        org_id=org_id, user_id=user_id, agent_id="agent-1", channel="web",
    )


async def test_a_string_session_id_finds_the_snapshot(
    session_store, session_factory,
):
    """The handler passes a str; the column is a UUID."""
    session = await _session(session_store, session_factory)
    await session_store.emit_event(
        session.id, EventType.TODO_UPDATED,
        {"todos": [{"id": "1", "content": "a", "status": "pending"}]},
    )

    got = await session_store.latest_todo_snapshot(str(session.id))
    assert got == [{"id": "1", "content": "a", "status": "pending"}]


async def test_the_newest_snapshot_wins(session_store, session_factory):
    session = await _session(session_store, session_factory)
    for n in ("1", "2", "3"):
        await session_store.emit_event(
            session.id, EventType.TODO_UPDATED,
            {"todos": [{"id": n, "content": n, "status": "pending"}]},
        )

    got = await session_store.latest_todo_snapshot(session.id)
    assert [t["id"] for t in got] == ["3"]


async def test_never_written_is_none_not_empty(session_store, session_factory):
    """``None`` and ``[]`` must stay distinct -- merge depends on it."""
    session = await _session(session_store, session_factory)
    assert await session_store.latest_todo_snapshot(session.id) is None


async def test_an_emptied_list_is_empty_not_none(session_store, session_factory):
    session = await _session(session_store, session_factory)
    await session_store.emit_event(
        session.id, EventType.TODO_UPDATED, {"todos": []},
    )
    assert await session_store.latest_todo_snapshot(session.id) == []


async def test_other_event_types_are_ignored(session_store, session_factory):
    session = await _session(session_store, session_factory)
    await session_store.emit_event(
        session.id, EventType.TOOL_RESULT,
        {"name": "todo", "content": '{"todos": [{"id": "x"}]}'},
    )
    assert await session_store.latest_todo_snapshot(session.id) is None


async def test_snapshots_are_scoped_to_one_session(
    session_store, session_factory,
):
    a = await _session(session_store, session_factory)
    b = await _session(session_store, session_factory)
    await session_store.emit_event(
        a.id, EventType.TODO_UPDATED,
        {"todos": [{"id": "1", "content": "a", "status": "pending"}]},
    )
    assert await session_store.latest_todo_snapshot(b.id) is None


async def test_unknown_session_is_none(session_store):
    assert await session_store.latest_todo_snapshot(uuid.uuid4()) is None
