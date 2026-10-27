"""A typed password stored before masking on write is masked on every read.

The runtime reads events through its store; the control plane selects
``events.data`` straight from the column. Both must get the masked copy.
"""

from __future__ import annotations

import json

import pytest
import sqlalchemy as sa

from surogates.db.models import Event

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

PASSWORD = "Legacy-Pass-1027"


async def test_a_legacy_typed_password_never_comes_back_from_the_database(
    session_store, session_factory,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        org_id=org_id, user_id=user_id, agent_id="agent-1", channel="web",
    )
    legacy = {"name": "browser_type", "arguments": {"ref": "@e5", "text": PASSWORD}}
    async with session_factory() as db:
        await db.execute(
            sa.text(
                "INSERT INTO events (session_id, type, data) "
                "VALUES (:sid, 'tool.call', CAST(:data AS jsonb))"
            ),
            {"sid": session.id, "data": json.dumps(legacy)},
        )
        await db.commit()

    [event] = await session_store.get_events(session.id)
    async with session_factory() as db:
        [column] = (
            await db.execute(sa.select(Event.data).where(Event.session_id == session.id))
        ).scalars().all()

    for data in (event.data, column):
        assert PASSWORD not in json.dumps(data)
        assert data["arguments"]["ref"] == "@e5"
