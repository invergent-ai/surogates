"""A step wakes the agent in a conversation that had gone idle, on Postgres and Redis.

The harness skips a wake for a ``completed`` session, so the waker must revive
the conversation the way an inbound message does before it queues it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from surogates.config import SHARED_WORK_QUEUE_KEY, encode_queue_member, enqueue_session
from surogates.db.models import (
    ProgramInvitationRow,
    ProgramOccurrenceRow,
    ProgramScheduleRow,
)
from surogates.programs.opener import make_step_waker
from surogates.session.events import EventType
from tests.integration.conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

AGENT = "a1"
CONFIG = {
    "name": "Daily check", "skill_ref": "daily-check", "channel": "telegram",
    "channel_identifier": "bot1", "timezone": "Europe/Bucharest",
    "weekdays": ["mon"], "times_local": ["09:00"], "response_deadline_hours": 24,
}


async def _invitation(session_factory, org_id, user_id, program_id, **fields):
    async with session_factory() as db:
        occ = ProgramOccurrenceRow(
            program_id=program_id, org_id=org_id, agent_id=AGENT,
            scheduled_for=datetime.now(timezone.utc), skill_ref="daily-check", status="fired",
        )
        db.add(occ)
        await db.flush()
        row = ProgramInvitationRow(
            occurrence_id=occ.id, program_id=program_id, org_id=org_id, agent_id=AGENT,
            user_id=user_id, platform="telegram", platform_user_id="555",
            channel_identifier="bot1", delivery_state="queued", escalation_state="none",
            **{"response_state": "awaiting_reply", **fields},
        )
        db.add(row)
        await db.commit()
        return row


async def test_a_step_revives_an_idle_conversation_before_waking_it(
    session_store, session_factory, redis_client,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    program_id = uuid.uuid4()
    async with session_factory() as db:
        db.add(ProgramScheduleRow(
            program_id=program_id, org_id=org_id, agent_id=AGENT, active=True, config=CONFIG,
        ))
        await db.commit()

    async def _wake_session(org, agent, session_id):
        await enqueue_session(redis_client, org_id=str(org), agent_id=agent, session_id=session_id)

    async def _config(pid):
        return CONFIG

    wake = make_step_waker(
        session_store=session_store, redis=redis_client, session_factory=session_factory,
        wake_session=_wake_session, config_for_program=_config,
    )

    first = await wake(await _invitation(session_factory, org_id, user_id, program_id), event="due")
    await session_store.update_session_status(first, "completed")

    due = await _invitation(session_factory, org_id, user_id, program_id)
    again = await wake(due, event="due")

    assert again == first  # the same conversation, not a new one
    assert (await session_store.get_session(again)).status == "active"
    events = await session_store.get_events(again)
    resumes = [e for e in events if e.type == EventType.SESSION_RESUME.value]
    assert resumes and resumes[-1].data == {"source": "program_step"}
    # Revived before the step context is written, as an inbound message is.
    context = [e for e in events if (e.data or {}).get("invitation_id") == str(due.id)]
    assert context and context[0].id > resumes[-1].id
    member = encode_queue_member(org_id=str(org_id), agent_id=AGENT, session_id=str(again))
    assert await redis_client.zscore(SHARED_WORK_QUEUE_KEY, member) is not None

    # The deadline wake revives the step's own conversation too.
    await session_store.update_session_status(again, "completed")
    async with session_factory() as db:
        row = await db.get(ProgramInvitationRow, due.id)
        row.response_state = "no_reply_by_deadline"
        await db.commit()
        row = await db.get(ProgramInvitationRow, due.id)
    assert await wake(row, event="deadline") == again
    assert (await session_store.get_session(again)).status == "active"
    deadline = [
        e for e in await session_store.get_events(again)
        if (e.data or {}).get("step_event") == "deadline"
    ]
    assert len(deadline) == 1
