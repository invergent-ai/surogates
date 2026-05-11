"""End-to-end tests for clarify responses through the inbox surface."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from surogates.db.models import Event
from surogates.session.events import EventType

from .inbox_e2e_helpers import create_user_token_session

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_clarify_through_inbox(
    inbox_client,
    session_factory,
    session_store,
):
    user_session = await create_user_token_session(session_factory, session_store)

    await session_store.emit_event(
        user_session.session.id,
        EventType.INBOX_INPUT_REQUIRED,
        {
            "tool_call_id": "tc-e2e-clarify",
            "questions": [{"prompt": "Pick a color"}],
            "context": "",
        },
    )

    list_response = await inbox_client.get(
        (
            "/v1/inbox?kind=input_required"
            f"&session_id={user_session.session.id}"
        ),
        headers=user_session.auth_headers,
    )

    assert list_response.status_code == 200, list_response.text
    items = list_response.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "input_required"
    assert item["status"] == "pending"
    assert item["action_ref"]["tool_call_id"] == "tc-e2e-clarify"

    response = await inbox_client.post(
        (
            f"/v1/sessions/{user_session.session.id}"
            "/clarify/tc-e2e-clarify/respond"
        ),
        json={"responses": [{"question": "Pick a color", "answer": "blue"}]},
        headers=user_session.auth_headers,
    )

    assert response.status_code == 201, response.text

    detail_response = await inbox_client.get(
        f"/v1/inbox/{item['id']}",
        headers=user_session.auth_headers,
    )
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()
    assert detail["status"] == "responded"
    assert detail["responded_at"] is not None

    async with session_store._sf() as db:
        clarify_event = (
            await db.execute(
                select(Event).where(
                    Event.session_id == user_session.session.id,
                    Event.type == EventType.CLARIFY_RESPONSE.value,
                )
            )
        ).scalar_one()

    assert clarify_event.data["tool_call_id"] == "tc-e2e-clarify"
    assert clarify_event.data["responses"] == [
        {"question": "Pick a color", "answer": "blue", "is_other": False}
    ]
