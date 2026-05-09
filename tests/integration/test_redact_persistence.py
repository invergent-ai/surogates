"""Integration coverage for redaction before event persistence."""

from __future__ import annotations

import json

import pytest

from surogates.session.events import EventType

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_session_store_redacts_event_data_before_persisting(
    session_store,
    session_factory,
) -> None:
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id="test-agent",
    )

    await session_store.emit_event(
        session.id,
        EventType.TOOL_RESULT,
        {
            "content": "leaked sk-proj-abcdefghijklmnopqrstuvwxyz123456",
            "nested": {"url": "https://example.test?refresh_token=refresh-secret"},
        },
    )

    events = await session_store.get_events(session.id)
    serialized = json.dumps(events[0].data)
    assert "abcdefghijklmnopqrstuvwxyz123456" not in serialized
    assert "refresh-secret" not in serialized
    assert "refresh_token=***" in serialized
