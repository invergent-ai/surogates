"""`session doctor` against a real SessionStore and PostgreSQL.

The unit tests drive a fake store and monkeypatch the pending-input lookup,
so neither the real `get_session` nor the real inbox query runs there. This
covers the wiring the CLI actually uses.
"""

from __future__ import annotations

import uuid

import pytest

from surogates.session.doctor import diagnose_session
from surogates.session.events import EventType

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _session(session_store, session_factory, **config):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    return await session_store.create_session(
        org_id=org_id, user_id=user_id, agent_id="agent-1",
        channel="web", config=config or {},
    )


async def _codes(session_store, session_id) -> list[str]:
    return [f.code for f in await diagnose_session(session_store, session_id)]


async def test_a_healthy_session_reports_nothing(session_store, session_factory):
    session = await _session(session_store, session_factory)
    assert await _codes(session_store, session.id) == []


async def test_an_unknown_session_is_reported(session_store):
    assert await _codes(session_store, uuid.uuid4()) == ["session_not_found"]


async def test_an_open_question_is_found_through_the_real_inbox(
    session_store, session_factory,
):
    """`inbox.input_required` auto-creates the row via `_INBOX_EVENTS`."""
    session = await _session(session_store, session_factory)
    await session_store.emit_event(
        session.id,
        EventType.INBOX_INPUT_REQUIRED,
        {
            "tool_call_id": "call_1",
            "questions": [{"prompt": "which one?"}],
            "context": "",
        },
    )

    assert "waiting_on_user" in await _codes(session_store, session.id)


async def test_two_objectives_at_once_is_reported(session_store, session_factory):
    session = await _session(
        session_store, session_factory,
        outcome={"description": "ship it", "status": "active"},
        active_mission_id=str(uuid.uuid4()),
    )
    assert "objective_conflict" in await _codes(session_store, session.id)


async def test_an_unusable_iteration_cap_is_reported(
    session_store, session_factory,
):
    session = await _session(session_store, session_factory, max_iterations=0)
    assert "unusable_max_iterations" in await _codes(session_store, session.id)
