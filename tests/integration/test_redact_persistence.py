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


async def test_delivery_data_reaches_the_outbox_but_never_the_event_log(
    session_store,
    session_factory,
) -> None:
    from sqlalchemy import select

    from surogates.db.models import DeliveryOutbox

    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent", channel="whatsapp",
        config={"whatsapp_channel_id": "40700000001", "channel_identifier": "pn1"},
    )
    masked = {"message": {"role": "assistant", "content": "Call +40…111"}}
    whole = {"message": {"role": "assistant", "content": "Call +40 711 111 111"}}

    event_id = await session_store.emit_event(
        session.id, EventType.LLM_RESPONSE, masked, delivery_data=whole,
    )

    events = await session_store.get_events(session.id)
    assert "711 111 111" not in json.dumps([e.data for e in events])
    async with session_factory() as db:
        outbox = (await db.execute(
            select(DeliveryOutbox).where(DeliveryOutbox.event_id == event_id)
        )).scalar_one()
    assert outbox.payload["content"] == "Call +40 711 111 111"


async def test_typed_text_is_masked_in_every_stored_copy_of_the_call(
    session_store,
    session_factory,
) -> None:
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )
    password = "Waalhaven-2026"

    await session_store.emit_event(session.id, EventType.LLM_RESPONSE, {"message": {
        "role": "assistant",
        "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "browser_type", "arguments": json.dumps({"ref": "@e5", "text": password}),
        }}],
    }})
    await session_store.emit_event(session.id, EventType.TOOL_CALL, {
        "tool_call_id": "c1", "name": "browser_type", "arguments": {"ref": "@e5", "text": password},
    })

    events = await session_store.get_events(session.id)
    assert password not in json.dumps([event.data for event in events])
    assert events[1].data["arguments"] == {"ref": "@e5", "text": "\u2022" * len(password)}
