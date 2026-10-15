"""The inbound pipeline records the window and delivers held outreach."""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from surogates.channels.inbound import ChannelInboundPipeline, InboundOutcome
from surogates.db.models import OutreachMessageRow
from surogates.followup import store
from surogates.session.events import EventType
from tests.test_channel_pipeline import (
    AGENT_ORG_ID,
    SESSION_ID,
    USER_ID,
    _make_config,
    _make_deps,
    _make_msg,
    _Routing,
)


def _whatsapp_routing():
    return _Routing(identifier="pn1", platform="whatsapp")


@pytest.mark.asyncio
async def test_an_inbound_message_opens_the_window_and_flushes_held_texts(sf):
    held = await store.queue_outreach(
        sf, org_id=AGENT_ORG_ID, agent_id=_whatsapp_routing().agent_id,
        user_id=USER_ID, platform="whatsapp", channel_identifier="pn1",
        kind="operator", text="Please call the practice today.",
        template_name="message", template_language="ro",
    )
    async with sf() as db:
        row = await db.get(OutreachMessageRow, held.id)
        row.state = "awaiting_reply"
        await db.commit()

    deps = dataclasses.replace(_make_deps(), session_factory=sf)
    msg = _make_msg(is_dm=True, platform_user_id="40711111111", text="ok")
    result = await ChannelInboundPipeline().handle(
        msg, routing=_whatsapp_routing(), config=_make_config(), deps=deps,
    )

    assert result == InboundOutcome.PROCESSED
    assert await store.last_inbound(
        sf, org_id=AGENT_ORG_ID, platform="whatsapp",
        platform_user_id="40711111111", channel_identifier="pn1",
    ) is not None
    types = [(e[1], e[2]) for e in deps.session_store.events]
    delivered = [d for t, d in types if t == EventType.LLM_RESPONSE]
    assert delivered[0]["message"]["content"] == "Please call the practice today."
    # Held text first, then the person's own message for the agent.
    assert [t for t, _ in types].index(EventType.LLM_RESPONSE) < [
        t for t, _ in types
    ].index(EventType.USER_MESSAGE)
    async with sf() as db:
        saved = await db.get(OutreachMessageRow, held.id)
    assert saved.state == "delivered"
    assert saved.session_id == SESSION_ID


@pytest.mark.asyncio
async def test_bookkeeping_failure_never_drops_the_message(sf, monkeypatch):
    from surogates.followup import store as followup_store

    async def broken(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(followup_store, "record_inbound", broken)
    deps = dataclasses.replace(_make_deps(), session_factory=sf)
    msg = _make_msg(is_dm=True, platform_user_id="40711111111", text="help")
    result = await ChannelInboundPipeline().handle(
        msg, routing=_whatsapp_routing(), config=_make_config(), deps=deps,
    )
    assert result == InboundOutcome.PROCESSED
    assert any(e[1] == EventType.USER_MESSAGE for e in deps.session_store.events)


@pytest.mark.asyncio
async def test_other_people_and_agents_keep_their_held_texts(sf):
    other = await store.queue_outreach(
        sf, org_id=AGENT_ORG_ID, agent_id="another-agent", user_id=USER_ID,
        platform="whatsapp", channel_identifier="pn1", kind="alert",
        text="not for this agent", template_name=None, template_language=None,
    )
    someone_else = await store.queue_outreach(
        sf, org_id=AGENT_ORG_ID, agent_id=_whatsapp_routing().agent_id,
        user_id=uuid.uuid4(), platform="whatsapp", channel_identifier="pn1",
        kind="alert", text="not for this person", template_name=None,
        template_language=None,
    )
    deps = dataclasses.replace(_make_deps(), session_factory=sf)
    await ChannelInboundPipeline().handle(
        _make_msg(is_dm=True, platform_user_id="40711111111"),
        routing=_whatsapp_routing(), config=_make_config(), deps=deps,
    )
    async with sf() as db:
        for row_id in (other.id, someone_else.id):
            assert (await db.get(OutreachMessageRow, row_id)).state == "queued"


async def _busy_line_case(sf, *, line, busy, messages=2):
    from datetime import datetime, timedelta, timezone

    from surogates.db.models import SessionLease

    await store.update_settings(
        sf, org_id=AGENT_ORG_ID, agent_id=_whatsapp_routing().agent_id,
        channel_identifier="pn1", texts={"busy_safety_line": line} if line else {},
    )
    if busy:
        async with sf() as db:
            db.add(SessionLease(
                session_id=SESSION_ID, owner_id="worker-1", lease_token=uuid.uuid4(),
                expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).replace(tzinfo=None),
            ))
            await db.commit()
    deps = dataclasses.replace(_make_deps(), session_factory=sf)
    nudges: list[str] = []

    async def _nudge(_session_id, _msg, text):
        nudges.append(text)

    deps.input_nudge = _nudge
    for text in ("one", "two")[:messages]:
        result = await ChannelInboundPipeline().handle(
            _make_msg(is_dm=True, platform_user_id="40711111111", text=text),
            routing=_whatsapp_routing(), config=_make_config(), deps=deps,
        )
        assert result == InboundOutcome.PROCESSED
    return nudges


@pytest.mark.asyncio
async def test_the_busy_line_goes_out_once_while_a_turn_is_running(sf):
    assert await _busy_line_case(sf, line="If it cannot wait, call the front desk.", busy=True) == [
        "If it cannot wait, call the front desk."
    ]


@pytest.mark.asyncio
async def test_no_busy_line_when_idle(sf):
    assert await _busy_line_case(sf, line="If it cannot wait, call the front desk.", busy=False) == []


@pytest.mark.asyncio
async def test_no_busy_line_unless_the_business_wrote_one(sf):
    from surogates.followup.wording import all_default_texts

    assert all_default_texts()["busy_safety_line"] == ""
    assert await _busy_line_case(sf, line=None, busy=True) == []
