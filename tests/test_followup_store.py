"""The follow-up store: enrollments, settings, contacts and outreach rows."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from surogates.db.models import ProgramScheduleRow
from surogates.followup import store

ORG = uuid.uuid4()
AGENT = "agent-1"


async def _enrol(sf, user, protocol="post-op", **kw):
    return await store.upsert_enrollment(
        sf, org_id=ORG, agent_id=AGENT, user_id=user, protocol=protocol,
        start_date=kw.get("start_date"), phase=kw.get("phase"),
        enrolled_by=kw.get("enrolled_by", "agent"),
    )


@pytest.mark.asyncio
async def test_enrol_then_list_active(sf):
    a, b = uuid.uuid4(), uuid.uuid4()
    await _enrol(sf, a, start_date=date(2026, 10, 1))
    await _enrol(sf, b)
    await _enrol(sf, uuid.uuid4(), protocol="other")
    rows = await store.active_enrollments(
        sf, org_id=ORG, agent_id=AGENT, protocol="post-op",
    )
    assert [r.user_id for r in rows] == [a, b]
    assert rows[0].start_date == date(2026, 10, 1)


@pytest.mark.asyncio
async def test_enrolling_again_reactivates_and_keeps_what_was_known(sf):
    user = uuid.uuid4()
    await _enrol(sf, user, start_date=date(2026, 10, 1), phase="acute")
    await store.update_enrollment(
        sf, org_id=ORG, agent_id=AGENT, user_id=user, protocol="post-op",
        status="ended", status_reason="moved away",
    )
    again = await _enrol(sf, user)
    assert again.status == "active"
    assert again.status_reason is None
    assert again.start_date == date(2026, 10, 1)
    assert again.phase == "acute"


@pytest.mark.asyncio
async def test_update_touches_only_what_is_passed(sf):
    user = uuid.uuid4()
    await _enrol(sf, user, start_date=date(2026, 10, 1), phase="acute")
    row = await store.update_enrollment(
        sf, org_id=ORG, agent_id=AGENT, user_id=user, protocol="post-op",
        phase=None,
    )
    assert row.phase is None
    assert row.start_date == date(2026, 10, 1)


@pytest.mark.asyncio
async def test_update_refuses_an_unknown_status(sf):
    user = uuid.uuid4()
    await _enrol(sf, user)
    with pytest.raises(ValueError):
        await store.update_enrollment(
            sf, org_id=ORG, agent_id=AGENT, user_id=user, protocol="post-op",
            status="finished",
        )


@pytest.mark.asyncio
async def test_update_of_a_missing_enrollment_returns_none(sf):
    assert await store.update_enrollment(
        sf, org_id=ORG, agent_id=AGENT, user_id=uuid.uuid4(),
        protocol="post-op", phase="x",
    ) is None


@pytest.mark.asyncio
async def test_offered_protocols_come_from_active_audience_programs(sf):
    async with sf() as db:
        for active, audience in [
            (True, {"protocol": "post-op", "day_to": 30}),
            (True, {"protocol": "post-op", "day_from": 31}),
            (True, {"protocol": "risk"}),
            (False, {"protocol": "paused-one"}),
            (True, None),
            (True, {"protocol": "", "day_to": 3}),
        ]:
            db.add(ProgramScheduleRow(
                program_id=uuid.uuid4(), org_id=ORG, agent_id=AGENT,
                active=active, config={"audience": audience},
            ))
        db.add(ProgramScheduleRow(
            program_id=uuid.uuid4(), org_id=ORG, agent_id="another-agent",
            active=True, config={"audience": {"protocol": "theirs"}},
        ))
        await db.commit()
    assert await store.offered_protocols(
        sf, org_id=ORG, agent_id=AGENT,
    ) == {"post-op", "risk"}


@pytest.mark.asyncio
async def test_settings_round_trip_dedupes_recipients(sf):
    sa_id, doc = uuid.uuid4(), uuid.uuid4()
    assert await store.get_settings(sf, org_id=ORG, agent_id=AGENT) is None
    saved = await store.save_settings(
        sf, org_id=ORG, agent_id=AGENT, escalation_service_account_id=sa_id,
        channel_identifier="pn1", alert_recipient_user_ids=[doc, str(doc)],
        alert_template_name="alert", alert_template_language="ro",
        message_template_name="", message_template_language=None,
    )
    assert saved.alert_recipient_user_ids == [str(doc)]
    assert saved.message_template_name is None
    loaded = await store.get_settings(sf, org_id=ORG, agent_id=AGENT)
    assert loaded.escalation_service_account_id == sa_id
    assert loaded.channel_identifier == "pn1"


@pytest.mark.asyncio
async def test_record_inbound_upserts_per_sender(sf):
    first = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
    key = dict(org_id=ORG, platform="whatsapp", platform_user_id="40700000001")
    await store.record_inbound(sf, **key, channel_identifier="pn1", at=first)
    await store.record_inbound(
        sf, **key, channel_identifier="pn1", at=first + timedelta(hours=1),
    )
    await store.record_inbound(sf, **key, channel_identifier="pn2", at=first)
    assert await store.last_inbound(
        sf, **key, channel_identifier="pn1",
    ) == first + timedelta(hours=1)
    by_user = await store.last_inbound_by_user(
        sf, org_id=ORG, platform="whatsapp", channel_identifier="pn2",
        platform_user_ids=["40700000001", "40700000002"],
    )
    assert by_user == {"40700000001": first}


@pytest.mark.asyncio
async def test_queue_outreach_validates(sf):
    base = dict(
        org_id=ORG, agent_id=AGENT, user_id=uuid.uuid4(), platform="whatsapp",
        channel_identifier="pn1", template_name=None, template_language=None,
    )
    with pytest.raises(ValueError):
        await store.queue_outreach(sf, **base, kind="spam", text="hi")
    with pytest.raises(ValueError):
        await store.queue_outreach(sf, **base, kind="alert", text="  ")
    row = await store.queue_outreach(sf, **base, kind="alert", text=" fever ")
    assert row.state == "queued"
    assert row.text == "fever"
